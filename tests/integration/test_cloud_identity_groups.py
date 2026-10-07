"""Real service-account signing and scheduler auth; Google HTTPS is intercepted."""

import asyncio
import json
import threading
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from urllib.parse import parse_qs

import httpx
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fl_common.errors import PlatformError
from fl_common.models.scheduler import Role
from fl_scheduler.auth.google import GoogleDirectory, Identity
from fl_scheduler.auth.group_directory import (
    GROUPS_SCOPE,
    CloudIdentityDirectory,
    load_group_directory,
)
from fl_scheduler.auth.groups import GroupAuthorizer, GroupSettings
from fl_scheduler.auth.models import HumanSession
from fl_scheduler.auth.sessions import Sessions
from google.auth import jwt
from sqlalchemy import select

EMAIL = "alice@example.edu"


@pytest.fixture
def group_credentials(tmp_path, monkeypatch):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private_key = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()
    public_key = (
        key.public_key()
        .public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
        .decode()
    )
    info = {
        "type": "service_account",
        "project_id": "test-project",
        "private_key_id": "test-key",
        "private_key": private_key,
        "client_email": "group-reader@test-project.iam.gserviceaccount.com",
        "client_id": "123456",
        "token_uri": "https://oauth2.googleapis.com/token",
    }
    path = tmp_path / "google-groups.json"
    path.write_text(json.dumps(info))
    path.chmod(0o600)
    fetched = []

    def token_request(url, **kwargs):
        assertion = parse_qs(kwargs["body"].decode())["assertion"][0]
        claims = jwt.decode(assertion, certs={"test-key": public_key}, audience=info["token_uri"])
        fetched.append((url, kwargs["timeout"], claims))
        return SimpleNamespace(
            status=200,
            headers={},
            data=json.dumps({"access_token": "test-access-token", "expires_in": 3600}).encode(),
        )

    monkeypatch.setattr("fl_scheduler.auth.group_directory.Request", lambda: token_request)
    return path, fetched


@pytest.fixture
async def cloud_groups(group_credentials):
    path, fetched = group_credentials
    state = {
        "members": {"users": {EMAIL}, "operators": set(), "admins": set()},
        "role": {"name": "MEMBER"},
        "group_status": 200,
        "lookup_status": 200,
        "membership_status": 200,
        "overrides": {},
    }
    requests = []

    def respond(request):
        requests.append(request)
        assert request.url.host == "cloudidentity.googleapis.com"
        assert request.headers["authorization"] == "Bearer test-access-token"
        resource = request.url.path.removeprefix("/v1/")
        if resource == "groups:lookup":
            stage = "group"
            value = {"name": "groups/" + request.url.params["groupKey.id"].split("@")[0]}
        elif resource.endswith("/memberships:lookup"):
            stage = "lookup"
            group = resource.split("/")[1]
            if request.url.params["memberKey.id"] not in state["members"].get(group, set()):
                return httpx.Response(404)
            value = {"name": resource.replace("memberships:lookup", "memberships/member-id")}
        else:
            stage = "membership"
            value = {
                "name": resource,
                "preferredMemberKey": {"id": EMAIL},
                "type": "USER",
                "roles": [state["role"]],
            }
        value.update(state["overrides"].get(stage, {}))
        return httpx.Response(
            state[stage + "_status"], json=value, headers={"Location": "https://evil.test/"}
        )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(respond), follow_redirects=True
    ) as c:
        yield CloudIdentityDirectory(path, c), state, requests, fetched


@pytest.mark.parametrize("role", ["MEMBER", "MANAGER", "OWNER"])
async def test_owner_credentials_sign_without_delegation_and_read_direct_roles(cloud_groups, role):
    directory, state, requests, fetched = cloud_groups
    state["role"] = {"name": role}
    assert await directory.has_member("users@example.edu", EMAIL)
    assert len(requests) == 3
    assert requests[0].url.params["groupKey.id"] == "users@example.edu"
    assert requests[1].url.params["memberKey.id"] == EMAIL
    assert fetched[0][0:2] == ("https://oauth2.googleapis.com/token", 15)
    assert fetched[0][2]["scope"] == GROUPS_SCOPE
    assert "sub" not in fetched[0][2]
    assert await directory.has_member("users@example.edu", EMAIL)
    assert len(fetched) == 1


@pytest.mark.parametrize("stage", ["lookup", "membership"])
async def test_missing_direct_member_and_removed_member_are_denied(cloud_groups, stage):
    directory, state, _, _ = cloud_groups
    state[stage + "_status"] = 404
    assert not await directory.has_member("users@example.edu", EMAIL)


@pytest.mark.parametrize("stage", ["group", "lookup", "membership"])
@pytest.mark.parametrize("status", [302, 403, 429, 500])
async def test_api_failures_do_not_follow_redirects_or_grant_access(cloud_groups, stage, status):
    directory, state, requests, _ = cloud_groups
    state[stage + "_status"] = status
    with pytest.raises(PlatformError) as error:
        await directory.has_member("users@example.edu", EMAIL)
    assert error.value.code == "AUTH_UNAVAILABLE"
    assert len(requests) == {"group": 1, "lookup": 2, "membership": 3}[stage]
    assert all(r.url.host == "cloudidentity.googleapis.com" for r in requests)


async def test_missing_group_is_an_outage_not_a_nonmember(cloud_groups):
    directory, state, _, _ = cloud_groups
    state["group_status"] = 404
    with pytest.raises(PlatformError) as error:
        await directory.has_member("users@example.edu", EMAIL)
    assert error.value.code == "AUTH_UNAVAILABLE"


@pytest.mark.parametrize(
    "stage,override",
    [
        ("group", {"name": "https://evil.test/"}),
        ("group", {"name": "groups/../other"}),
        ("group", {"name": "groups/users?token=evil"}),
        ("lookup", {"name": "groups/admins/memberships/member-id"}),
        ("lookup", {"name": "groups/users/memberships/../../other"}),
        ("membership", {"name": "groups/other/memberships/member-id"}),
        ("membership", {"preferredMemberKey": {"id": "mallory@example.edu"}}),
        ("membership", {"preferredMemberKey": {"id": EMAIL, "namespace": "other"}}),
        ("membership", {"type": "GROUP"}),
        ("membership", {"roles": []}),
        ("membership", {"roles": [{"name": "MEMBER"}, {"name": "MEMBER"}]}),
        ("membership", {"roles": [{"name": "UNKNOWN"}]}),
        ("membership", {"roles": [{"name": "OWNER", "expiryDetail": {}}]}),
        ("membership", {"roles": [{"name": "MEMBER", "expiryDetail": {"expireTime": "bad"}}]}),
        (
            "membership",
            {"roles": [{"name": "MEMBER", "expiryDetail": {"expireTime": "2099-01-01T00:00:00"}}]},
        ),
    ],
)
async def test_malformed_resources_and_memberships_fail_closed(cloud_groups, stage, override):
    directory, state, requests, _ = cloud_groups
    state["overrides"][stage] = override
    with pytest.raises(PlatformError) as error:
        await directory.has_member("users@example.edu", EMAIL)
    assert error.value.code == "AUTH_UNAVAILABLE"
    assert len(requests) == {"group": 1, "lookup": 2, "membership": 3}[stage]


@pytest.mark.parametrize("seconds,expected", [(-60, False), (600, True)])
async def test_expiring_membership(cloud_groups, seconds, expected):
    directory, state, _, _ = cloud_groups
    state["role"] = {
        "name": "MEMBER",
        "expiryDetail": {
            "expireTime": (datetime.now(UTC) + timedelta(seconds=seconds)).isoformat()
        },
    }
    assert await directory.has_member("users@example.edu", EMAIL) is expected


async def test_cloud_identity_roles_removal_and_outage_with_real_sessions(
    cloud_groups, scheduler_db
):
    directory, state, _, _ = cloud_groups
    groups = GroupAuthorizer(directory, GroupSettings("users", "operators", "admins", 0))
    sessions = Sessions(scheduler_db, groups)
    identity = Identity("google-alice", EMAIL)
    tokens = await sessions.issue(identity)
    access = tokens.access_token.get_secret_value()
    assert (await sessions.authenticate(access)).role == Role.USER
    state["members"]["operators"].add(EMAIL)
    assert (await sessions.authenticate(access)).role == Role.OPERATOR
    state["members"]["admins"].add(EMAIL)
    assert (await sessions.authenticate(access)).role == Role.ADMIN
    state["group_status"] = 403
    with pytest.raises(PlatformError) as error:
        await sessions.authenticate(access)
    assert error.value.code == "AUTH_UNAVAILABLE"
    state["group_status"] = 200
    for members in state["members"].values():
        members.clear()
    with pytest.raises(PlatformError) as error:
        await sessions.refresh(tokens.refresh_token.get_secret_value())
    assert error.value.code == "FORBIDDEN"


async def test_cloud_identity_nonmember_never_creates_session(cloud_groups, scheduler_db):
    directory, _, _, _ = cloud_groups
    sessions = Sessions(scheduler_db, GroupAuthorizer(directory, GroupSettings("users")))
    with pytest.raises(PlatformError) as error:
        await sessions.issue(Identity("google-mallory", "mallory@example.edu"))
    assert error.value.code == "FORBIDDEN"
    with scheduler_db.transaction() as db:
        assert db.scalar(select(HumanSession)) is None


async def test_backend_selection_without_admin_and_legacy_compatibility(
    group_credentials, monkeypatch
):
    path, _ = group_credentials
    monkeypatch.delenv("FL_GOOGLE_DELEGATED_ADMIN", raising=False)
    monkeypatch.delenv("FL_GOOGLE_DIRECTORY_CREDENTIALS", raising=False)
    monkeypatch.setenv("FL_GOOGLE_GROUPS_BACKEND", "cloud_identity")
    monkeypatch.setenv("FL_GOOGLE_GROUPS_CREDENTIALS", str(path))
    async with httpx.AsyncClient() as client:
        assert isinstance(load_group_directory(client), CloudIdentityDirectory)
        monkeypatch.delenv("FL_GOOGLE_GROUPS_BACKEND")
        monkeypatch.setenv("FL_GOOGLE_DIRECTORY_CREDENTIALS", str(path))
        with pytest.raises(RuntimeError, match="FL_GOOGLE_DELEGATED_ADMIN"):
            load_group_directory(client)
        monkeypatch.setenv("FL_GOOGLE_DELEGATED_ADMIN", "admin@example.edu")
        assert isinstance(load_group_directory(client), GoogleDirectory)
        monkeypatch.setenv("FL_GOOGLE_GROUPS_BACKEND", "unknown")
        with pytest.raises(RuntimeError, match="cloud_identity or directory"):
            load_group_directory(client)


@pytest.mark.parametrize("unsafe", ["permissions", "symlink"])
async def test_owner_credential_file_must_be_private(group_credentials, unsafe, tmp_path):
    path, _ = group_credentials
    if unsafe == "permissions":
        path.chmod(0o644)
    else:
        link = tmp_path / "link.json"
        link.symlink_to(path)
        path = link
    async with httpx.AsyncClient() as client:
        with pytest.raises((PermissionError, OSError)):
            CloudIdentityDirectory(path, client)


async def test_cancelled_refresh_finishes_before_next_refresh(cloud_groups, monkeypatch):
    directory, _, _, _ = cloud_groups
    entered, release = threading.Event(), threading.Event()
    original = directory.credentials.refresh
    calls = []

    def refresh(request):
        calls.append("refresh")
        entered.set()
        assert release.wait(5)
        original(request)

    monkeypatch.setattr(directory.credentials, "refresh", refresh)
    first = asyncio.create_task(directory.has_member("users", EMAIL))
    assert await asyncio.to_thread(entered.wait, 5)
    first.cancel()
    second = asyncio.create_task(directory.has_member("users", EMAIL))
    try:
        await asyncio.sleep(0.02)
        assert not first.done() and not second.done()
        assert calls == ["refresh"]
    finally:
        release.set()
        await asyncio.gather(first, second, return_exceptions=True)
    assert first.cancelled()
    assert second.result() is True
    assert calls == ["refresh"]
