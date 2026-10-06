"""Human enrollment administration and scoped bootstrap/agent credential endpoints."""

import asyncio
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Request
from fl_common.errors import PlatformError
from fl_common.models import ClusterConfig
from fl_common.models.base import Schema
from fl_common.models.enrollment import EnrollmentClaim, EnrollmentRegistration
from fl_common.models.scheduler import ClusterSnapshot, Principal
from pydantic import Field
from sqlalchemy import select

from ..auth.api import AuthAPI
from .enrollment import EnrollmentService
from .models import Enrollment


class IssueRequest(Schema):
    lifetime_seconds: int = Field(default=1800, ge=60, le=3600)


def bearer(request: Request) -> str:
    scheme, _, token = request.headers.get("authorization", "").partition(" ")
    if scheme.lower() != "bearer" or not 32 <= len(token) <= 256:
        raise PlatformError("UNAUTHENTICATED", "A scoped Bearer credential is required")
    return token


def routes(auth: AuthAPI, enrollment: EnrollmentService | None) -> APIRouter:
    router = APIRouter(prefix="/api", tags=["enrollment"])

    def configured() -> EnrollmentService:
        if enrollment is None:
            raise PlatformError("NETWORK_UNAVAILABLE", "Headscale enrollment is not configured")
        return enrollment

    @router.post("/admin/enrollments", status_code=201)
    async def issue(
        body: IssueRequest, principal: Annotated[Principal, Depends(auth.principal)]
    ) -> dict[str, object]:
        ticket = await configured().issue(principal, body.lifetime_seconds)
        return ticket.wire()

    @router.get("/admin/enrollments")
    async def tickets(
        principal: Annotated[Principal, Depends(auth.principal)],
    ) -> list[dict[str, str]]:
        service = configured()
        service._admin(principal)

        def read() -> list[dict[str, str]]:
            with service.db.transaction() as session:
                return [
                    {
                        "enrollment_id": str(row.enrollment_id),
                        "cluster_id": str(row.cluster_id),
                        "state": row.state,
                        "expires_at": row.expires_at.isoformat(),
                        "created_by": row.created_by,
                    }
                    for row in session.scalars(
                        select(Enrollment).order_by(Enrollment.expires_at.desc()).limit(500)
                    )
                ]

        return await asyncio.to_thread(read)

    @router.delete("/admin/enrollments/{enrollment_id}", status_code=202)
    async def revoke(
        enrollment_id: UUID, principal: Annotated[Principal, Depends(auth.principal)]
    ) -> dict[str, str]:
        await configured().revoke(enrollment_id, principal)
        return {"status": "revoked"}

    @router.post("/enrollment/claim")
    async def claim(body: EnrollmentClaim, request: Request) -> dict[str, object]:
        grant = await configured().claim(bearer(request), body.bootstrap_secret.get_secret_value())
        return grant.wire()

    @router.post("/enrollment/register")
    async def register(body: EnrollmentRegistration, request: Request) -> ClusterConfig:
        return await configured().register(bearer(request), body)

    @router.post("/agents/{cluster_id}/unregister", status_code=202)
    @router.post("/enrollment/clusters/{cluster_id}/unregister", status_code=202)
    async def unregister(
        cluster_id: UUID, body: ClusterSnapshot, request: Request
    ) -> dict[str, str]:
        await asyncio.to_thread(configured().unregister, cluster_id, bearer(request), body)
        return {"status": "unregistered"}

    return router
