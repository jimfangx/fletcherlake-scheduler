"""Linux service entrypoint. A reverse proxy terminates public HTTPS on port 443."""

import os
from pathlib import Path

import httpx
import uvicorn
from fastapi import FastAPI
from fl_common.models.transfer import TransferEndpoint
from pydantic import SecretStr
from sqlalchemy import text

from .artifacts.api import Downloads
from .artifacts.service import ExportService
from .auth.api import AuthAPI
from .auth.google import GoogleDirectory, GoogleIdentity, GoogleSettings
from .auth.groups import GroupAuthorizer, GroupSettings
from .auth.login import Login
from .auth.sessions import Sessions
from .dashboard import mount_dashboard
from .db.core import Database
from .network.enrollment import EnrollmentService
from .network.headscale import Headscale
from .notifications.config import load_settings
from .notifications.service import Notifications
from .service import create_app
from .transfers.gateway import GatewayControl
from .transfers.service import TransferService
from .transfers.submissions import Submissions


def required(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(f"Required service environment variable is missing: {name}")
    return value


def application() -> FastAPI:
    origin = required("FL_PUBLIC_ORIGIN")
    settings = GoogleSettings(
        client_id=required("FL_GOOGLE_CLIENT_ID"),
        client_secret=SecretStr(required("FL_GOOGLE_CLIENT_SECRET")),
        callback_url=origin + "/api/auth/callback",
        workspace_domain=os.environ.get("FL_GOOGLE_WORKSPACE_DOMAIN"),
    )
    directory_file = Path(required("FL_GOOGLE_DIRECTORY_CREDENTIALS"))
    if directory_file.stat().st_mode & 0o077:
        raise RuntimeError("Directory service-account credentials must have mode 0600")
    db = Database(required("FL_DATABASE_URL"))
    # Schema upgrades are an explicit deployment step, never a side effect of HTTP requests.
    with db.transaction() as transaction:
        version = transaction.scalar(text("SELECT version_num FROM alembic_version"))
        if version != "0010_notification_delivery":
            raise RuntimeError("Run the scheduler Alembic upgrade before starting this service")
    client = httpx.AsyncClient(timeout=15)
    provider = GoogleIdentity(settings, client)
    directory = GoogleDirectory(directory_file, required("FL_GOOGLE_DELEGATED_ADMIN"), client)
    groups = GroupAuthorizer(
        directory,
        GroupSettings(
            users=required("FL_GOOGLE_USERS_GROUP"),
            operators=os.environ.get("FL_GOOGLE_OPERATORS_GROUP"),
            admins=os.environ.get("FL_GOOGLE_ADMINS_GROUP"),
        ),
    )
    sessions = Sessions(db, groups)
    auth = AuthAPI(sessions, Login(db, provider, sessions), origin)
    network = Headscale(
        required("FL_HEADSCALE_ADMIN_URL"),
        required("FL_HEADSCALE_LOGIN_URL"),
        SecretStr(required("FL_HEADSCALE_API_KEY")),
        client,
    )
    enrollment = EnrollmentService(
        db,
        network,
        network.login_url,
        SecretStr(required("FL_ENROLLMENT_ENCRYPTION_KEY")),
        agent_origin=required("FL_AGENT_ORIGIN"),
    )

    transfers = TransferService(
        db,
        GatewayControl(
            required("FL_TRANSFER_GATEWAY_ORIGIN"),
            SecretStr(required("FL_TRANSFER_GATEWAY_CONTROL_SECRET")),
            client,
        ),
        TransferEndpoint.model_validate_json(
            Path(required("FL_TRANSFER_PRIVATE_ENDPOINT_FILE")).read_text()
        ),
    )

    submissions = Submissions(
        db,
        transfers,
        TransferEndpoint.model_validate_json(
            Path(required("FL_TRANSFER_PUBLIC_ENDPOINT_FILE")).read_text()
        ),
        required("FL_TRANSFER_PUBLIC_ORIGIN"),
    )
    downloads = Downloads(
        ExportService(db, transfers.gateway, transfers.endpoint),
        submissions.uploads.endpoint,
    )
    notification_file = os.environ.get("FL_NOTIFICATION_CONFIG_FILE")
    notifications = (
        Notifications(db, load_settings(Path(notification_file)), client, origin)
        if notification_file
        else None
    )

    async def shutdown() -> None:
        await client.aclose()
        db.close()

    app = create_app(
        db,
        auth,
        shutdown=shutdown,
        enrollment=enrollment,
        transfers=transfers,
        submissions=submissions,
        downloads=downloads,
        notifications=notifications,
    )
    dashboard = os.environ.get("FL_DASHBOARD_DIR")
    if dashboard:
        mount_dashboard(app, Path(dashboard))
    return app


def main() -> None:
    uvicorn.run(
        "fl_scheduler.server:application",
        factory=True,
        host=os.environ.get("FL_BIND_HOST", "127.0.0.1"),
        port=int(os.environ.get("FL_BIND_PORT", "8080")),
        # Query strings on the OAuth callback contain one-use codes. Proxy logs must omit them too.
        access_log=False,
        proxy_headers=True,
        forwarded_allow_ips="127.0.0.1",
    )


if __name__ == "__main__":
    main()
