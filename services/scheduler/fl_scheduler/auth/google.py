"""Google's code exchange/ID-token verification and Workspace group membership.

Directory credentials use domain-wide delegation to a Workspace administrator.
Google access/refresh tokens are never returned to platform clients or persisted.
"""

import asyncio
import hashlib
import secrets
from base64 import urlsafe_b64encode
from dataclasses import dataclass
from functools import partial
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import quote, urlencode, urlsplit

import httpx
from fl_common.errors import PlatformError
from google.auth.exceptions import GoogleAuthError, TransportError
from google.auth.transport.requests import Request
from google.oauth2 import id_token, service_account
from pydantic import SecretStr


@dataclass(frozen=True)
class Identity:
    subject: str
    email: str


class IdentityProvider(Protocol):
    def authorization_url(self, state: str, nonce: str, verifier: str) -> str: ...

    async def exchange(self, code: str, nonce: str, verifier: str) -> Identity: ...


class GroupDirectory(Protocol):
    async def has_member(self, group: str, email: str) -> bool: ...


@dataclass(frozen=True)
class GoogleSettings:
    client_id: str
    client_secret: SecretStr
    callback_url: str
    workspace_domain: str | None = None

    def __post_init__(self) -> None:
        url = urlsplit(self.callback_url)
        if url.scheme != "https" or not url.hostname or url.username or url.fragment:
            raise ValueError("Google callback must be a public HTTPS URL")


class GoogleIdentity:
    def __init__(self, settings: GoogleSettings, client: httpx.AsyncClient) -> None:
        self.settings = settings
        self.client = client

    def authorization_url(self, state: str, nonce: str, verifier: str) -> str:
        challenge = urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=")
        query = urlencode(
            {
                "client_id": self.settings.client_id,
                "redirect_uri": self.settings.callback_url,
                "response_type": "code",
                "scope": "openid email",
                "state": state,
                "nonce": nonce,
                "code_challenge": challenge.decode(),
                "code_challenge_method": "S256",
                "prompt": "select_account",
            }
        )
        return "https://accounts.google.com/o/oauth2/v2/auth?" + query

    async def exchange(self, code: str, nonce: str, verifier: str) -> Identity:
        try:
            response = await self.client.post(
                "https://oauth2.googleapis.com/token",
                data={
                    "code": code,
                    "client_id": self.settings.client_id,
                    "client_secret": self.settings.client_secret.get_secret_value(),
                    "redirect_uri": self.settings.callback_url,
                    "grant_type": "authorization_code",
                    "code_verifier": verifier,
                },
                timeout=15,
            )
            response.raise_for_status()
            token = response.json()["id_token"]
            claims = await asyncio.to_thread(self._verify, token)
            return self._identity(claims, nonce)
        except TransportError as error:
            raise PlatformError(
                "AUTH_UNAVAILABLE", "Google token verification is unavailable"
            ) from error
        except (httpx.HTTPError, GoogleAuthError, ValueError, KeyError, TypeError) as error:
            raise PlatformError("UNAUTHENTICATED", "Google login could not be verified") from error

    def _verify(self, token: str) -> dict[str, Any]:
        # Google's maintained verifier checks signature, expiry, issuer, and audience.
        return dict(
            id_token.verify_oauth2_token(  # type: ignore[no-untyped-call]
                token, partial(Request(), timeout=15), self.settings.client_id
            )
        )

    def _identity(self, claims: dict[str, Any], nonce: str) -> Identity:
        if not isinstance(claims.get("nonce"), str) or not secrets.compare_digest(
            claims["nonce"], nonce
        ):
            raise ValueError("ID token nonce mismatch")
        email, subject = claims.get("email"), claims.get("sub")
        if claims.get("email_verified") is not True or not isinstance(email, str):
            raise ValueError("Google email is not verified")
        if not isinstance(subject, str) or not subject or len(subject) > 255:
            raise ValueError("Invalid Google subject")
        if len(email) > 320 or "@" not in email:
            raise ValueError("Invalid email")
        domain = claims.get("hd")
        if self.settings.workspace_domain and domain != self.settings.workspace_domain:
            raise ValueError("Wrong Workspace organization")
        # Google is not authoritative for arbitrary historical third-party email claims.
        if not domain and not email.lower().endswith("@gmail.com"):
            raise ValueError("A Gmail or Workspace identity is required")
        return Identity(subject, email.lower())


class GoogleDirectory:
    def __init__(
        self, credentials_file: Path, delegated_admin: str, client: httpx.AsyncClient
    ) -> None:
        self.credentials = service_account.Credentials.from_service_account_file(  # type: ignore[no-untyped-call]
            str(credentials_file),
            scopes=["https://www.googleapis.com/auth/admin.directory.group.member.readonly"],
            subject=delegated_admin,
        )
        self.client = client
        self.lock = asyncio.Lock()

    async def has_member(self, group: str, email: str) -> bool:
        try:
            async with self.lock:
                if not self.credentials.valid:
                    await asyncio.to_thread(
                        self.credentials.refresh, partial(Request(), timeout=15)
                    )
                token = self.credentials.token
            url = "https://admin.googleapis.com/admin/directory/v1/groups/"
            url += quote(group, safe="") + "/hasMember/" + quote(email, safe="")
            response = await self.client.get(
                url, headers={"Authorization": f"Bearer {token}"}, timeout=15
            )
            response.raise_for_status()
            value = response.json()["isMember"]
            if not isinstance(value, bool):
                raise ValueError("Invalid Directory response")
            return value
        except Exception as error:
            # Never reuse expired positive membership during a Directory outage.
            raise PlatformError("AUTH_UNAVAILABLE", "Google Groups lookup failed") from error
