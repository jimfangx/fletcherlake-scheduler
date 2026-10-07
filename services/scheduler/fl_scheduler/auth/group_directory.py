"""Select delegated Directory or group-owned Cloud Identity membership checks."""

import asyncio
import json
import os
import re
from datetime import UTC, datetime
from functools import partial
from pathlib import Path
from typing import Any

import httpx
from fl_common.async_calls import background_call
from fl_common.errors import PlatformError
from fl_common.private_files import read_private
from google.auth.transport.requests import Request
from google.oauth2 import service_account

from .google import GoogleDirectory, GroupDirectory

GROUPS_SCOPE = "https://www.googleapis.com/auth/cloud-identity.groups.readonly"
API = "https://cloudidentity.googleapis.com/v1/"
RESOURCE_ID = r"[A-Za-z0-9_-]+"


class CloudIdentityDirectory:
    """Read direct human memberships as a service account owning specific groups.

    No administrator impersonation, domain-wide delegation or transitive-membership
    premium API is used. Inaccessible groups and malformed responses fail closed.
    """

    def __init__(self, credentials_file: Path, client: httpx.AsyncClient) -> None:
        self.credentials = service_account.Credentials.from_service_account_info(  # type: ignore[no-untyped-call]
            json.loads(read_private(credentials_file)), scopes=[GROUPS_SCOPE]
        )
        self.client = client
        self.lock = asyncio.Lock()

    async def _get(
        self,
        resource: str,
        token: str,
        *,
        params: dict[str, str] | None = None,
        missing_member: bool = False,
    ) -> dict[str, Any] | None:
        response = await self.client.get(
            API + resource,
            params=params,
            headers={"Authorization": f"Bearer {token}"},
            timeout=15,
            follow_redirects=False,
        )
        if missing_member and response.status_code == 404:
            return None
        response.raise_for_status()
        value = response.json()
        if not isinstance(value, dict):
            raise ValueError("Invalid Cloud Identity response")
        return value

    async def has_member(self, group: str, email: str) -> bool:
        try:
            async with self.lock:
                if not self.credentials.valid:
                    await background_call(self.credentials.refresh, partial(Request(), timeout=15))
                token = self.credentials.token
            if not isinstance(token, str) or not token:
                raise ValueError("Missing Cloud Identity access token")
            # Resolve again on each uncached authorization; never pin a recreated group.
            parent = await self._get("groups:lookup", token, params={"groupKey.id": group})
            if parent is None or not isinstance(name := parent.get("name"), str):
                raise ValueError("Missing group resource")
            if not re.fullmatch(r"groups/" + RESOURCE_ID, name):
                raise ValueError("Invalid group resource")
            lookup = await self._get(
                name + "/memberships:lookup",
                token,
                params={"memberKey.id": email},
                missing_member=True,
            )
            if lookup is None:
                return False
            membership = lookup.get("name")
            if not isinstance(membership, str) or not re.fullmatch(
                re.escape(name) + r"/memberships/" + RESOURCE_ID, membership
            ):
                raise ValueError("Invalid membership resource")
            value = await self._get(membership, token, missing_member=True)
            if value is None:
                return False
            key = value.get("preferredMemberKey")
            if (
                value.get("name") != membership
                or value.get("type") != "USER"
                or not isinstance(key, dict)
                or key.get("namespace") not in (None, "")
                or not isinstance(key.get("id"), str)
                or key["id"].lower() != email.lower()
            ):
                raise ValueError("Membership identity mismatch")
            roles = value.get("roles")
            if not isinstance(roles, list) or not roles:
                raise ValueError("Missing membership roles")
            active = False
            seen: set[str] = set()
            for role in roles:
                if not isinstance(role, dict) or role.get("name") not in (
                    "OWNER",
                    "MANAGER",
                    "MEMBER",
                ):
                    raise ValueError("Invalid membership role")
                role_name = role["name"]
                if role_name in seen:
                    raise ValueError("Duplicate membership role")
                seen.add(role_name)
                expiry = role.get("expiryDetail")
                if expiry is None:
                    active = True
                else:
                    if role_name != "MEMBER" or not isinstance(expiry, dict):
                        raise ValueError("Invalid membership expiry")
                    expires = datetime.fromisoformat(expiry["expireTime"])
                    if expires.tzinfo is None:
                        raise ValueError("Membership expiry must have a timezone")
                    active |= expires > datetime.now(UTC)
            return active
        except Exception as error:
            raise PlatformError("AUTH_UNAVAILABLE", "Google Groups lookup failed") from error


def load_group_directory(client: httpx.AsyncClient) -> GroupDirectory:
    """Keep existing delegated configurations working; explicitly opt in to owner access."""

    def required(name: str) -> str:
        value = os.environ.get(name)
        if not value:
            raise RuntimeError(f"Required service environment variable is missing: {name}")
        return value

    backend = os.environ.get("FL_GOOGLE_GROUPS_BACKEND", "directory")
    if backend == "cloud_identity":
        return CloudIdentityDirectory(Path(required("FL_GOOGLE_GROUPS_CREDENTIALS")), client)
    if backend == "directory":
        path = Path(required("FL_GOOGLE_DIRECTORY_CREDENTIALS"))
        if path.stat().st_mode & 0o077:
            raise RuntimeError("Directory service-account credentials must have mode 0600")
        return GoogleDirectory(path, required("FL_GOOGLE_DELEGATED_ADMIN"), client)
    raise RuntimeError("FL_GOOGLE_GROUPS_BACKEND must be cloud_identity or directory")
