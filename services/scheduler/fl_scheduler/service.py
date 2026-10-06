"""Scheduler HTTP composition and periodic metadata maintenance.

This service never owns hardware. Agents keep executing when its process stops.
Transfer delivery uses private gateway control and durable agent commands; reservations
precede payload IO.
"""

import asyncio
import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response
from fl_common.errors import PlatformError

from .agents.commands import Commands
from .agents.gateway import AgentGateway
from .agents.reconcile import Reconciler
from .api.control import Control
from .api.queries import Queries
from .api.routes import routes
from .artifacts.api import Downloads
from .artifacts.api import routes as artifact_routes
from .auth.api import AuthAPI
from .db.core import Database
from .logs.api import routes as log_routes
from .logs.cleanup import sweep as sweep_logs
from .logs.reads import LogReads
from .network.api import routes as enrollment_routes
from .network.cleanup import NetworkCleanup
from .network.enrollment import EnrollmentService
from .notifications.alerts import Alerts
from .notifications.api import routes as notification_routes
from .notifications.service import Notifications
from .scheduler.placement import Placement
from .transfers.api import routes as transfer_routes
from .transfers.service import TransferService
from .transfers.submission_api import routes as submission_routes
from .transfers.submissions import Submissions
from .transfers.upload_cleanup import UploadCleanup

logger = logging.getLogger(__name__)


class Maintenance:
    def __init__(self, db: Database, auth: AuthAPI) -> None:
        self.reconciler = Reconciler(db)
        self.placement = Placement(db)
        self.auth = auth
        self.commands = Commands(db)
        self.alerts = Alerts(db)

    def tick(self) -> None:
        self.reconciler.health_sweep()
        self.placement.reserve_pending()
        self.commands.enqueue_empty()
        self.auth.login.sweep()
        sweep_logs(self.commands.db)
        self.alerts.sweep()

    async def run(self) -> None:
        while True:
            tick = asyncio.create_task(asyncio.to_thread(self.tick))
            try:
                await asyncio.shield(tick)
            except asyncio.CancelledError:
                # Reap the database thread before service shutdown closes its connection pool.
                await asyncio.gather(tick, return_exceptions=True)
                raise
            except Exception as error:
                # Secret-bearing database/provider exceptions must not enter public logs.
                logger.error("Scheduler maintenance failed (%s)", type(error).__name__)
            await asyncio.sleep(5)


def create_app(
    db: Database,
    auth: AuthAPI,
    *,
    maintenance: bool = True,
    shutdown: Callable[[], Awaitable[None]] | None = None,
    enrollment: EnrollmentService | None = None,
    transfers: TransferService | None = None,
    submissions: Submissions | None = None,
    downloads: Downloads | None = None,
    notifications: Notifications | None = None,
) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        task = asyncio.create_task(Maintenance(db, auth).run()) if maintenance else None
        network_task = (
            asyncio.create_task(NetworkCleanup(enrollment).run())
            if maintenance and enrollment
            else None
        )
        transfer_task = asyncio.create_task(transfers.run()) if maintenance and transfers else None
        export_task = (
            asyncio.create_task(downloads.exports.run()) if maintenance and downloads else None
        )
        upload_task = (
            asyncio.create_task(UploadCleanup(db, submissions.transfers.gateway).run())
            if maintenance and submissions
            else None
        )
        notification_task = (
            asyncio.create_task(notifications.run()) if maintenance and notifications else None
        )
        try:
            yield
        finally:
            if task:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            if network_task:
                network_task.cancel()
                await asyncio.gather(network_task, return_exceptions=True)
            if transfer_task:
                transfer_task.cancel()
                await asyncio.gather(transfer_task, return_exceptions=True)
            if export_task:
                export_task.cancel()
                await asyncio.gather(export_task, return_exceptions=True)
            if upload_task:
                upload_task.cancel()
                await asyncio.gather(upload_task, return_exceptions=True)
            if notification_task:
                notification_task.cancel()
                await asyncio.gather(notification_task, return_exceptions=True)
            if shutdown:
                await shutdown()

    app = FastAPI(title="Fletcherlake scheduler", lifespan=lifespan)
    app.include_router(auth.router)
    app.include_router(routes(auth, Queries(db), Placement(db), Control(db)))
    app.include_router(AgentGateway(db).router)
    app.include_router(enrollment_routes(auth, enrollment))
    app.include_router(transfer_routes(auth, transfers))
    app.include_router(submission_routes(auth, submissions))
    app.include_router(artifact_routes(auth, downloads))
    app.include_router(log_routes(auth, LogReads(db)))
    app.include_router(notification_routes(auth, db))

    @app.exception_handler(PlatformError)
    async def platform_error(request: Request, error: PlatformError) -> JSONResponse:
        statuses = {
            "UNAUTHENTICATED": 401,
            "FORBIDDEN": 403,
            "AUTH_UNAVAILABLE": 503,
            "JOB_NOT_FOUND": 404,
            "CLUSTER_NOT_FOUND": 404,
            "BOARD_NOT_FOUND": 404,
            "ARTIFACT_NOT_FOUND": 404,
            "NOTIFICATION_NOT_FOUND": 404,
            "SLOW_DOWN": 429,
            "NETWORK_UNAVAILABLE": 503,
            "UNAUTHORIZED_AGENT": 401,
        }
        return JSONResponse(error.as_dict(), status_code=statuses.get(error.code, 409))

    @app.middleware("http")
    async def headers(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        return response

    @app.get("/healthz", include_in_schema=False)
    async def health() -> dict[str, str]:
        from sqlalchemy import text

        def check() -> None:
            with db.transaction() as session:
                session.execute(text("SELECT 1"))

        await asyncio.to_thread(check)
        return {"status": "ok"}

    return app
