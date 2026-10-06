"""Browser OAuth with PKCE/nonce/state binding, and terminal login approval.

Terminal clients hold a private polling secret. Humans approve the separately
displayed user code after Google login, so opening an attacker-supplied login URL
cannot silently authorize another terminal. Challenges survive scheduler restarts.
"""

import asyncio
import secrets
from dataclasses import dataclass
from datetime import timedelta

from fl_common.errors import PlatformError
from fl_common.models.base import utcnow
from sqlalchemy import delete, select

from ..db.core import Database
from .google import Identity, IdentityProvider
from .models import DeviceLogin, HumanSession, LoginState
from .sessions import Sessions, Tokens, digest


@dataclass(frozen=True)
class BrowserLogin:
    url: str
    browser_secret: str


@dataclass(frozen=True)
class TerminalLogin:
    device_secret: str
    user_code: str
    expires_in: int = 600
    interval: int = 5


class Login:
    def __init__(self, db: Database, provider: IdentityProvider, sessions: Sessions) -> None:
        self.db = db
        self.provider = provider
        self.sessions = sessions

    def begin(self, return_path: str = "/") -> BrowserLogin:
        if return_path not in {"/", "/api/auth/terminal/verify"}:
            raise ValueError("Unsupported login return path")
        state, browser, nonce, verifier = (secrets.token_urlsafe(32) for _ in range(4))
        with self.db.transaction() as session:
            session.add(
                LoginState(
                    state_hash=digest(state),
                    browser_hash=digest(browser),
                    nonce=nonce,
                    verifier=verifier,
                    expires_at=utcnow() + timedelta(minutes=10),
                    return_path=return_path,
                )
            )
        return BrowserLogin(self.provider.authorization_url(state, nonce, verifier), browser)

    def _consume(self, state: str, browser: str) -> tuple[str, str, str]:
        with self.db.transaction() as session:
            row = session.scalar(
                select(LoginState).where(LoginState.state_hash == digest(state)).with_for_update()
            )
            if (
                row is None
                or row.expires_at <= utcnow()
                or not secrets.compare_digest(row.browser_hash, digest(browser))
            ):
                raise PlatformError("UNAUTHENTICATED", "Invalid or expired browser login")
            nonce, verifier, return_path = row.nonce, row.verifier, row.return_path
            session.delete(row)
            return nonce, verifier, return_path

    async def callback(self, state: str, browser: str, code: str) -> tuple[Tokens, str]:
        nonce, verifier, return_path = await asyncio.to_thread(self._consume, state, browser)
        identity = await self.provider.exchange(code, nonce, verifier)
        await self.sessions.groups.principal(identity)
        tokens = await asyncio.to_thread(self.sessions.issue_authorized, identity)
        return tokens, return_path

    def terminal(self) -> TerminalLogin:
        secret = secrets.token_urlsafe(48)
        # 56 bits of entropy; expiration and edge rate limits also bound guesses.
        code = secrets.token_hex(7).upper()
        code = code[:7] + "-" + code[7:]
        with self.db.transaction() as session:
            session.add(
                DeviceLogin(
                    secret_hash=digest(secret),
                    user_code=code,
                    expires_at=utcnow() + timedelta(minutes=10),
                )
            )
        return TerminalLogin(secret, code)

    def approve(self, user_code: str, identity: Identity) -> None:
        with self.db.transaction() as session:
            row = session.scalar(
                select(DeviceLogin)
                .where(DeviceLogin.user_code == user_code.strip().upper())
                .with_for_update()
            )
            if row is None or row.expires_at <= utcnow() or row.consumed_at or row.subject:
                raise PlatformError("LOGIN_EXPIRED", "Terminal code is unavailable")
            row.subject, row.email = identity.subject, identity.email

    def _poll(self, secret: str) -> Identity:
        with self.db.transaction() as session:
            row = session.scalar(
                select(DeviceLogin)
                .where(DeviceLogin.secret_hash == digest(secret))
                .with_for_update()
            )
            if row is None or row.expires_at <= utcnow() or row.consumed_at:
                raise PlatformError("LOGIN_EXPIRED", "Terminal login expired or was consumed")
            if row.last_poll_at and (utcnow() - row.last_poll_at).total_seconds() < 5:
                raise PlatformError("SLOW_DOWN", "Poll no more than once every five seconds")
            row.last_poll_at = utcnow()
            if not row.subject or not row.email:
                pending = True
                identity = None
            else:
                pending = False
                identity = Identity(row.subject, row.email)
                row.consumed_at = utcnow()
        # Commit rate limiting even when authorization is pending.
        if pending or identity is None:
            raise PlatformError("AUTHORIZATION_PENDING", "Approve the terminal code in the browser")
        return identity

    async def poll(self, secret: str) -> Tokens:
        identity = await asyncio.to_thread(self._poll, secret)
        await self.sessions.groups.principal(identity)
        return await asyncio.to_thread(self.sessions.issue_authorized, identity)

    def sweep(self) -> None:
        """Periodic bounded retention of authentication state, unrelated to job history."""
        with self.db.transaction() as session:
            for model in (LoginState, DeviceLogin):
                session.execute(delete(model).where(model.expires_at <= utcnow()))
            session.execute(delete(HumanSession).where(HumanSession.refresh_expires_at <= utcnow()))
