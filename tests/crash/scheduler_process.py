"""Production HTTP/WSS and maintenance composition with injected identity providers."""

import json
import sys
from pathlib import Path

import uvicorn
from fl_scheduler.auth.api import AuthAPI
from fl_scheduler.auth.groups import GroupAuthorizer, GroupSettings
from fl_scheduler.auth.login import Login
from fl_scheduler.auth.sessions import Sessions
from fl_scheduler.db.core import Database
from fl_scheduler.service import create_app

from tests.auth import Directory, Provider


def main():
    root = Path(sys.argv[1])
    settings = json.loads((root / "scheduler.json").read_text())
    db = Database(settings["database_url"])
    groups = GroupAuthorizer(Directory(), GroupSettings("users", "operators", "admins", 0))
    sessions = Sessions(db, groups)
    auth = AuthAPI(sessions, Login(db, Provider(), sessions), settings["origin"])

    async def shutdown():
        db.close()

    app = create_app(db, auth, shutdown=shutdown)
    uvicorn.run(
        app,
        host="127.0.0.1",
        port=settings["port"],
        ssl_certfile=str(root / "server.pem"),
        ssl_keyfile=str(root / "server.key"),
        log_level="warning",
        access_log=False,
        timeout_graceful_shutdown=3,
    )


if __name__ == "__main__":
    main()
