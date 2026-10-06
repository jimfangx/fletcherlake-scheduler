"""Real PostgreSQL persistence and HTTP boundaries; Google is an injected provider."""

from datetime import timedelta
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from fl_common.errors import PlatformError
from fl_common.models.base import utcnow
from fl_common.models.scheduler import Role
from fl_scheduler.auth.google import GoogleIdentity, GoogleSettings, Identity
from fl_scheduler.auth.groups import GroupSettings
from fl_scheduler.auth.models import DeviceLogin, HumanSession
from fl_scheduler.auth.sessions import digest
from pydantic import SecretStr
from sqlalchemy import select


async def test_google_group_roles_and_removed_membership(auth_stack):
    sessions, _, directory, provider, _ = auth_stack
    tokens = await sessions.issue(provider.identity)
    access = tokens.access_token.get_secret_value()
    assert (await sessions.authenticate(access)).role == Role.USER
    directory.members["operators"].add(provider.identity.email)
    assert (await sessions.authenticate(access)).role == Role.OPERATOR
    directory.members["admins"].add(provider.identity.email)
    assert (await sessions.authenticate(access)).role == Role.ADMIN
    for members in directory.members.values():
        members.clear()
    with pytest.raises(PlatformError, match="not in an authorized group"):
        await sessions.authenticate(access)
    with pytest.raises(PlatformError, match="not in an authorized group"):
        await sessions.refresh(tokens.refresh_token.get_secret_value())


async def test_directory_outage_fails_closed_after_cache_expiry(auth_stack):
    sessions, _, directory, provider, _ = auth_stack
    sessions.groups.settings = GroupSettings("users", cache_seconds=60)
    tokens = await sessions.issue(provider.identity)
    directory.unavailable = True
    assert (await sessions.authenticate(tokens.access_token.get_secret_value())).role == Role.USER
    sessions.groups.cache.clear()
    with pytest.raises(PlatformError, match="Directory unavailable"):
        await sessions.authenticate(tokens.access_token.get_secret_value())


async def test_browser_state_binding_replay_and_pkce(auth_stack):
    sessions, login, _, provider, _ = auth_stack
    begun = login.begin()
    state = parse_qs(urlsplit(begun.url).query)["state"][0]
    with pytest.raises(PlatformError, match="browser login"):
        await login.callback(state, "different browser", "code")
    tokens, return_path = await login.callback(state, begun.browser_secret, "code")
    assert return_path == "/"
    assert (
        await sessions.authenticate(tokens.access_token.get_secret_value())
    ).subject == "google-alice"
    assert len(provider.exchanges[0][1]) >= 32
    assert len(provider.exchanges[0][2]) >= 32
    with pytest.raises(PlatformError, match="browser login"):
        await login.callback(state, begun.browser_secret, "code")
    assert len(provider.exchanges) == 1


async def test_non_group_login_never_creates_session(auth_stack, scheduler_db):
    _, login, _, provider, _ = auth_stack
    provider.identity = Identity("mallory", "mallory@example.edu")
    begun = login.begin()
    state = parse_qs(urlsplit(begun.url).query)["state"][0]
    with pytest.raises(PlatformError, match="not in an authorized group"):
        await login.callback(state, begun.browser_secret, "code")
    with scheduler_db.transaction() as db:
        assert db.scalar(select(HumanSession)) is None


async def test_refresh_rotation_one_use_and_expiry(auth_stack, scheduler_db):
    sessions, _, _, provider, _ = auth_stack
    original = await sessions.issue(provider.identity)
    rotated = await sessions.refresh(original.refresh_token.get_secret_value())
    with pytest.raises(PlatformError, match="credential"):
        await sessions.authenticate(original.access_token.get_secret_value())
    with pytest.raises(PlatformError, match="credential"):
        await sessions.refresh(original.refresh_token.get_secret_value())
    assert (
        await sessions.authenticate(rotated.access_token.get_secret_value())
    ).email == provider.identity.email
    with scheduler_db.transaction() as db:
        row = db.scalar(select(HumanSession))
        assert row.access_hash == digest(rotated.access_token.get_secret_value())
        assert rotated.access_token.get_secret_value() not in repr(row.__dict__)
        row.access_expires_at = utcnow() - timedelta(seconds=1)
    with pytest.raises(PlatformError, match="credential"):
        await sessions.authenticate(rotated.access_token.get_secret_value())
    renewed = await sessions.refresh(rotated.refresh_token.get_secret_value())
    sessions.revoke(renewed.access_token.get_secret_value())
    with pytest.raises(PlatformError, match="credential"):
        await sessions.refresh(renewed.refresh_token.get_secret_value())


async def test_terminal_approval_secret_is_one_use_and_polling_is_limited(auth_stack, scheduler_db):
    sessions, login, _, provider, _ = auth_stack
    terminal = login.terminal()
    with pytest.raises(PlatformError) as error:
        await login.poll(terminal.device_secret)
    assert error.value.code == "AUTHORIZATION_PENDING"
    with pytest.raises(PlatformError) as error:
        await login.poll(terminal.device_secret)
    assert error.value.code == "SLOW_DOWN"
    login.approve(terminal.user_code, provider.identity)
    with scheduler_db.transaction() as db:
        db.scalar(select(DeviceLogin)).last_poll_at = utcnow() - timedelta(seconds=6)
    tokens = await login.poll(terminal.device_secret)
    assert (
        await sessions.authenticate(tokens.access_token.get_secret_value())
    ).email == provider.identity.email
    with pytest.raises(PlatformError) as error:
        await login.poll(terminal.device_secret)
    assert error.value.code == "LOGIN_EXPIRED"


async def test_browser_cookie_security_and_origin_enforcement(auth_stack):
    _, login, _, _, app = auth_stack
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="https://scheduler.test"
    ) as client:
        begun = await client.get("/api/auth/login")
        assert "Secure" in begun.headers["set-cookie"] and "HttpOnly" in begun.headers["set-cookie"]
        state = parse_qs(urlsplit(begun.headers["location"]).query)["state"][0]
        callback = await client.get("/api/auth/callback", params={"state": state, "code": "code"})
        assert callback.status_code == 303
        assert "access_token" not in callback.text
        assert (await client.get("/api/auth/me")).status_code == 200
        terminal = login.terminal()
        body = {"user_code": terminal.user_code}
        assert (await client.post("/api/auth/terminal/approve", json=body)).status_code == 403
        assert (
            await client.post(
                "/api/auth/terminal/approve", json=body, headers={"Origin": "https://evil.test"}
            )
        ).status_code == 403
        assert (
            await client.post(
                "/api/auth/terminal/approve",
                json=body,
                headers={"Origin": "https://scheduler.test"},
            )
        ).status_code == 204
        assert (
            await client.post("/api/auth/logout", headers={"Origin": "https://scheduler.test"})
        ).status_code == 204
        assert (await client.get("/api/auth/me")).status_code == 401


async def test_google_adapter_exchanges_pkce_and_checks_verified_claims(monkeypatch):
    settings = GoogleSettings(
        "client-id",
        SecretStr("client-secret"),
        "https://scheduler.test/api/auth/callback",
        "example.edu",
    )
    requests = []

    def transport(request):
        requests.append(request)
        return httpx.Response(200, json={"id_token": "signed-token"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
        google = GoogleIdentity(settings, client)
        claims = {
            "sub": "alice-sub",
            "email": "Alice@example.edu",
            "email_verified": True,
            "hd": "example.edu",
            "nonce": "expected",
        }
        monkeypatch.setattr(google, "_verify", lambda token: dict(claims))
        identity = await google.exchange("code", "expected", "verifier")
        assert identity.email == "alice@example.edu"
        assert b"code_verifier=verifier" in requests[0].content
        query = parse_qs(urlsplit(google.authorization_url("state", "nonce", "verifier")).query)
        assert query["code_challenge_method"] == ["S256"]
        assert "verifier" not in query["code_challenge"]
        for field, bad in [("nonce", "wrong"), ("email_verified", False), ("hd", "evil.test")]:
            original = claims[field]
            claims[field] = bad
            with pytest.raises(PlatformError, match="could not be verified"):
                await google.exchange("code", "expected", "verifier")
            claims[field] = original
