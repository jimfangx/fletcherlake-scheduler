"""Opaque access tokens and single-use rotating refresh tokens.

Group roles are checked on every authenticated request through the brief cache;
neither a stored session nor a user profile preserves withdrawn membership.
"""

import asyncio
import hashlib
import secrets
from datetime import datetime, timedelta
from uuid import UUID

from fl_common.errors import PlatformError
from fl_common.models.base import Schema, utcnow
from fl_common.models.scheduler import Principal
from pydantic import AwareDatetime, SecretStr
from sqlalchemy import select

from ..db.core import Database
from ..db.models import User
from .google import Identity
from .groups import GroupAuthorizer
from .models import HumanSession


def digest(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


class Tokens(Schema):
    access_token: SecretStr
    refresh_token: SecretStr
    access_expires_at: AwareDatetime
    refresh_expires_at: AwareDatetime

    def wire(self) -> dict[str, str]:
        """Only explicit credential responses expose secrets; model repr/dumps mask them."""
        return {
            "access_token": self.access_token.get_secret_value(),
            "refresh_token": self.refresh_token.get_secret_value(),
            "access_expires_at": self.access_expires_at.isoformat(),
            "refresh_expires_at": self.refresh_expires_at.isoformat(),
            "token_type": "Bearer",
        }


def new_tokens(refresh_expires: datetime | None = None) -> Tokens:
    return Tokens(
        access_token=SecretStr(secrets.token_urlsafe(32)),
        refresh_token=SecretStr(secrets.token_urlsafe(48)),
        access_expires_at=utcnow() + timedelta(minutes=15),
        refresh_expires_at=refresh_expires or utcnow() + timedelta(days=7),
    )


class Sessions:
    def __init__(self, db: Database, groups: GroupAuthorizer) -> None:
        self.db = db
        self.groups = groups

    async def issue(self, identity: Identity) -> Tokens:
        await self.groups.principal(identity)
        return await asyncio.to_thread(self.issue_authorized, identity)

    def issue_authorized(self, identity: Identity) -> Tokens:
        """Internal only: caller must already have authorized this verified identity."""
        tokens = new_tokens()
        with self.db.transaction() as session:
            user = session.scalar(select(User).where(User.subject == identity.subject))
            email_user = session.get(User, identity.email)
            if (user and user.email != identity.email) or (
                email_user and email_user.subject != identity.subject
            ):
                # Email renames require explicit migration of historical ownership records.
                raise PlatformError(
                    "IDENTITY_CHANGED", "Account identity requires administrator review"
                )
            if user is None:
                user = User(email=identity.email, subject=identity.subject)
                session.add(user)
            user.last_login_at = utcnow()
            session.flush()
            session.add(
                HumanSession(
                    subject=identity.subject,
                    access_hash=digest(tokens.access_token.get_secret_value()),
                    refresh_hash=digest(tokens.refresh_token.get_secret_value()),
                    access_expires_at=tokens.access_expires_at,
                    refresh_expires_at=tokens.refresh_expires_at,
                )
            )
        return tokens

    def _lookup(self, token: str, *, refresh: bool = False) -> tuple[UUID, Identity]:
        if not 32 <= len(token) <= 256:
            raise PlatformError("UNAUTHENTICATED", "Invalid or expired credential")
        with self.db.transaction() as session:
            column = HumanSession.refresh_hash if refresh else HumanSession.access_hash
            row = session.scalar(select(HumanSession).where(column == digest(token)))
            expiry = None
            if row:
                expiry = row.refresh_expires_at if refresh else row.access_expires_at
            if row is None or row.revoked_at or expiry is None or expiry <= utcnow():
                raise PlatformError("UNAUTHENTICATED", "Invalid or expired credential")
            user = session.scalar(select(User).where(User.subject == row.subject))
            assert user is not None
            return row.session_id, Identity(user.subject, user.email)

    async def authenticate(self, token: str) -> Principal:
        _, identity = await asyncio.to_thread(self._lookup, token)
        return await self.groups.principal(identity)

    async def refresh(self, token: str) -> Tokens:
        session_id, identity = await asyncio.to_thread(self._lookup, token, refresh=True)
        await self.groups.principal(identity)
        return await asyncio.to_thread(self._rotate, session_id, token)

    def _rotate(self, session_id: UUID, token: str) -> Tokens:
        with self.db.transaction() as session:
            row = session.scalar(
                select(HumanSession).where(HumanSession.session_id == session_id).with_for_update()
            )
            if (
                row is None
                or row.revoked_at
                or row.refresh_expires_at <= utcnow()
                or not secrets.compare_digest(row.refresh_hash, digest(token))
            ):
                raise PlatformError("UNAUTHENTICATED", "Refresh credential was already used")
            tokens = new_tokens(row.refresh_expires_at)
            row.access_hash = digest(tokens.access_token.get_secret_value())
            row.refresh_hash = digest(tokens.refresh_token.get_secret_value())
            row.access_expires_at = tokens.access_expires_at
            return tokens

    def revoke(self, token: str) -> None:
        with self.db.transaction() as session:
            row = session.scalar(
                select(HumanSession)
                .where(HumanSession.access_hash == digest(token))
                .with_for_update()
            )
            if row:
                row.revoked_at = utcnow()
