"""Real HTTPS scheduler for browser acceptance; identity/network providers are injected."""

import json
from contextlib import ExitStack, contextmanager
from urllib.parse import urlencode

from cryptography.fernet import Fernet
from fastapi import FastAPI
from fastapi.responses import RedirectResponse
from fl_common.files import atomic_write
from fl_common.models.base import utcnow
from fl_scheduler.agents.reconcile import Reconciler
from fl_scheduler.auth.api import AuthAPI
from fl_scheduler.auth.google import Identity
from fl_scheduler.dashboard import mount_dashboard
from fl_scheduler.network.enrollment import EnrollmentService
from fl_scheduler.registry import Registry
from fl_scheduler.service import create_app
from pydantic import SecretStr

from tests.dashboard_https import certificate, serve_https
from tests.dashboard_snapshot import snapshot
from tests.network_helpers import MockNetwork


@contextmanager
def browser_server(auth_stack, db, config, root, dist):
    sessions, login, directory, provider, _ = auth_stack
    directory.members["admins"].add("alice@example.edu")
    directory.members["users"].add("bob@example.edu")
    sessions.issue_authorized(provider.identity)
    bob = sessions.issue_authorized(Identity("google-bob", "bob@example.edu"))
    registered = Registry(db).register(config, "fixture-agent-token")
    data = snapshot(registered)
    data.timestamp = utcnow()
    data.jobs[0].spec.owner = "alice@example.edu"
    data.jobs[1].spec.owner = "bob@example.edu"
    reconciler = Reconciler(db)
    connection = reconciler.connected(registered.cluster_id)
    reconciler.snapshot(registered.cluster_id, connection, data)
    network = EnrollmentService(
        db,
        MockNetwork(),
        "https://headscale.test",
        SecretStr(Fernet.generate_key().decode()),
        agent_origin="https://agent.scheduler.test",
    )
    certificate(root)
    with ExitStack() as stack:
        google_origin = ""

        def scheduler(origin):
            def google(issuer):
                nonlocal google_origin
                google_origin = issuer
                provider.authorization_url = lambda state, nonce, verifier: (
                    issuer + "/authorize?" + urlencode({"state": state})
                )
                app = FastAPI()

                @app.get("/authorize")
                def authorize(state: str):
                    return RedirectResponse(
                        origin
                        + "/api/auth/callback?"
                        + urlencode({"state": state, "code": "fixture-code"}),
                        status_code=303,
                    )

                return app

            stack.enter_context(serve_https(google, root, hostname="localhost"))
            app = create_app(
                db, AuthAPI(sessions, login, origin), maintenance=False, enrollment=network
            )
            mount_dashboard(app, dist)
            return app

        origin = stack.enter_context(serve_https(scheduler, root))
        fixture = {
            "origin": origin,
            "google_origin": google_origin,
            "cluster_id": str(registered.cluster_id),
            "job_id": str(data.jobs[0].spec.job_id),
            "bob_job_id": str(data.jobs[1].spec.job_id),
            "bob_token": bob.access_token.get_secret_value(),
        }
        fixture_path = root / "browser-fixture.json"
        atomic_write(fixture_path, json.dumps(fixture).encode())
        yield fixture_path
