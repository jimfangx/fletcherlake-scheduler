"""Authenticated HTTPS metadata calls to the private transfer gateway control API."""

from uuid import UUID

import httpx
from fl_common.errors import PlatformError
from fl_common.models import ArtifactRef
from fl_common.models.transfer import TransferGrant, TransferStatus
from fl_common.network import https_origin
from pydantic import SecretStr, TypeAdapter


class GatewayControl:
    def __init__(self, origin: str, secret: SecretStr, client: httpx.AsyncClient) -> None:
        self.origin, self.secret, self.client = https_origin(origin), secret, client

    async def request(
        self,
        method: str,
        path: str,
        grant: TransferGrant | None = None,
        *,
        http_timeout: float = 15,
    ) -> httpx.Response:
        try:
            response = await self.client.request(
                method,
                self.origin + path,
                headers={"Authorization": "Bearer " + self.secret.get_secret_value()},
                json=grant.model_dump(mode="json") if grant else None,
                timeout=http_timeout,
                follow_redirects=False,
            )
            if not 200 <= response.status_code < 300:
                raise PlatformError(
                    "TRANSFER_GATEWAY_UNAVAILABLE", "Gateway control operation failed"
                )
            return response
        except httpx.HTTPError:
            raise PlatformError(
                "TRANSFER_GATEWAY_UNAVAILABLE", "Gateway control operation failed"
            ) from None

    async def status(self, transfer_id: UUID) -> TransferStatus:
        response = await self.request("GET", f"/internal/transfers/{transfer_id}")
        try:
            return TransferStatus.model_validate_json(response.content)
        except ValueError:
            raise PlatformError(
                "TRANSFER_GATEWAY_PROTOCOL", "Gateway returned malformed metadata"
            ) from None

    async def register(self, grant: TransferGrant) -> None:
        await self.request("PUT", "/internal/transfers", grant)

    async def verify(self, transfer_id: UUID) -> list[ArtifactRef]:
        response = await self.request(
            "POST", f"/internal/transfers/{transfer_id}/verify", http_timeout=600
        )
        return TypeAdapter(list[ArtifactRef]).validate_json(response.content)

    async def revoke(self, transfer_id: UUID) -> None:
        await self.request("DELETE", f"/internal/transfers/{transfer_id}")
