"""Injected identity/directory providers for public API tests against real PostgreSQL."""

from urllib.parse import urlencode

import pytest
from fl_common.errors import PlatformError
from fl_scheduler.auth.api import AuthAPI
from fl_scheduler.auth.google import Identity
from fl_scheduler.auth.groups import GroupAuthorizer, GroupSettings
from fl_scheduler.auth.login import Login
from fl_scheduler.auth.sessions import Sessions
from fl_scheduler.service import create_app


class Directory:
    def __init__(self):
        self.members = {"users": {"alice@example.edu"}, "operators": set(), "admins": set()}
        self.calls = 0
        self.unavailable = False

    async def has_member(self, group, email):
        self.calls += 1
        if self.unavailable:
            raise PlatformError("AUTH_UNAVAILABLE", "Directory unavailable")
        return email in self.members[group]


class Provider:
    def __init__(self):
        self.identity = Identity("google-alice", "alice@example.edu")
        self.exchanges = []

    def authorization_url(self, state, nonce, verifier):
        return "https://google.test/authorize?" + urlencode({"state": state})

    async def exchange(self, code, nonce, verifier):
        self.exchanges.append((code, nonce, verifier))
        return self.identity


@pytest.fixture
def auth_stack(scheduler_db):
    directory = Directory()
    groups = GroupAuthorizer(directory, GroupSettings("users", "operators", "admins", 0))
    sessions = Sessions(scheduler_db, groups)
    provider = Provider()
    login = Login(scheduler_db, provider, sessions)
    api = AuthAPI(sessions, login, "https://scheduler.test")
    return sessions, login, directory, provider, create_app(scheduler_db, api, maintenance=False)
