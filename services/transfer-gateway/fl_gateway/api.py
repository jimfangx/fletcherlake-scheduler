"""Private scheduler control and a public, narrowly scoped verification endpoint."""

import hashlib
import secrets
from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from typing import Annotated
from uuid import UUID

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse, Response
from fl_common.errors import PlatformError
from fl_common.models import ArtifactRef
from fl_common.models.transfer import TransferGrant, TransferStatus
from pydantic import SecretStr

from .store import GatewayStore


def bearer(value: str | None) -> str:
    if value is None or not value.startswith("Bearer ") or not 32 <= len(value[7:]) <= 512:
        raise HTTPException(401, "A transfer credential is required")
    return value[7:]


def create_app(
    store: GatewayStore,
    control_secret: SecretStr,
    *,
    lifespan: Callable[[FastAPI], AbstractAsyncContextManager[None]] | None = None,
) -> FastAPI:
    if len(control_secret.get_secret_value()) < 32:
        raise ValueError("Gateway control credentials require at least 32 characters")

    # A custom lifecycle is supplied only by the service composition.
    @asynccontextmanager
    async def no_lifecycle(app: FastAPI) -> AsyncIterator[None]:
        yield

    app = FastAPI(title="Fletcherlake transfer gateway", lifespan=lifespan or no_lifecycle)

    def admin(authorization: Annotated[str | None, Header()] = None) -> None:
        if not secrets.compare_digest(bearer(authorization), control_secret.get_secret_value()):
            raise HTTPException(401, "Invalid gateway control credential")

    @app.exception_handler(PlatformError)
    async def platform_error(request: Request, error: PlatformError) -> JSONResponse:
        if error.code == "ALREADY_OWNED":
            return JSONResponse(
                {"code": "TRANSFER_BUSY", "message": "Retry this transfer"}, status_code=409
            )
        statuses = {"TRANSFER_UNAUTHORIZED": 401, "TRANSFER_NOT_FOUND": 404}
        return JSONResponse(error.as_dict(), status_code=statuses.get(error.code, 409))

    @app.put("/internal/transfers", dependencies=[Depends(admin)], status_code=204)
    def register(grant: TransferGrant) -> Response:
        store.register(grant)
        return Response(status_code=204)

    @app.get("/internal/transfers/{transfer_id}", dependencies=[Depends(admin)])
    def status(transfer_id: UUID) -> TransferStatus:
        grant, state = store.lookup(transfer_id, active=False)
        return TransferStatus.model_validate({"grant": grant, "state": state})

    @app.delete("/internal/transfers/{transfer_id}", dependencies=[Depends(admin)], status_code=204)
    def revoke(transfer_id: UUID) -> Response:
        store.revoke(transfer_id)
        return Response(status_code=204)

    @app.post("/internal/transfers/{transfer_id}/verify", dependencies=[Depends(admin)])
    def seal(transfer_id: UUID) -> list[ArtifactRef]:
        grant, _ = store.lookup(transfer_id)
        return store.verify(transfer_id, grant.token_hash)

    @app.post("/api/uploads/{transfer_id}/verify")
    def verify(
        transfer_id: UUID, authorization: Annotated[str | None, Header()] = None
    ) -> list[ArtifactRef]:
        digest = hashlib.sha256(bearer(authorization).encode()).hexdigest()
        grant, _ = store.lookup(transfer_id)
        if grant.source_networks:
            raise PlatformError(
                "TRANSFER_UNAUTHORIZED", "Private publication requires scheduler sealing"
            )
        return store.verify(transfer_id, digest)

    @app.get("/healthz", include_in_schema=False)
    def health() -> dict[str, str]:
        with store.connection() as connection:
            connection.execute("SELECT 1")
        return {"status": "ok"}

    return app
