"""Human HTTP authentication. Browser mutations require the configured Origin."""

import asyncio
from typing import Annotated, Literal
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse
from fl_common.errors import PlatformError
from fl_common.models.base import Schema
from fl_common.models.scheduler import Principal
from pydantic import Field, SecretStr

from .google import Identity
from .login import Login
from .sessions import Sessions, Tokens

SESSION_COOKIE = "__Host-fl-session"
REFRESH_COOKIE = "__Secure-fl-refresh"
LOGIN_COOKIE = "__Host-fl-login"


class CredentialRequest(Schema):
    credential: SecretStr = Field(min_length=32, max_length=256)


class ApprovalRequest(Schema):
    user_code: str = Field(min_length=1, max_length=32)


class AuthAPI:
    def __init__(self, sessions: Sessions, login: Login, public_origin: str) -> None:
        url = urlsplit(public_origin)
        if (
            url.scheme != "https"
            or not url.hostname
            or url.username
            or url.path
            or url.query
            or url.fragment
        ):
            raise ValueError("Public origin must be HTTPS without a trailing slash")
        self.sessions, self.login, self.public_origin = sessions, login, public_origin
        self.router = APIRouter(prefix="/api/auth", tags=["authentication"])
        self._routes()

    def check_origin(self, request: Request) -> None:
        if request.headers.get("origin") != self.public_origin:
            raise PlatformError("FORBIDDEN", "Browser mutation requires the scheduler Origin")

    async def principal(self, request: Request) -> Principal:
        authorization = request.headers.get("authorization", "")
        if authorization:
            scheme, _, token = authorization.partition(" ")
            if scheme.lower() != "bearer" or not token:
                raise PlatformError("UNAUTHENTICATED", "Use a Bearer credential")
        else:
            token = request.cookies.get(SESSION_COOKIE, "")
            if request.method not in {"GET", "HEAD", "OPTIONS"}:
                self.check_origin(request)
        return await self.sessions.authenticate(token)

    @staticmethod
    def _cookies(response: Response, tokens: Tokens) -> None:
        response.set_cookie(
            SESSION_COOKIE,
            tokens.access_token.get_secret_value(),
            secure=True,
            httponly=True,
            samesite="lax",
            max_age=900,
            path="/",
        )
        response.set_cookie(
            REFRESH_COOKIE,
            tokens.refresh_token.get_secret_value(),
            secure=True,
            httponly=True,
            samesite="strict",
            max_age=7 * 86400,
            path="/api/auth",
        )

    def _routes(self) -> None:
        @self.router.get("/login")
        async def begin(
            return_to: Literal["/", "/api/auth/terminal/verify"] = "/",
        ) -> RedirectResponse:
            login = await asyncio.to_thread(self.login.begin, return_to)
            response = RedirectResponse(login.url, status_code=303)
            response.set_cookie(
                LOGIN_COOKIE,
                login.browser_secret,
                secure=True,
                httponly=True,
                samesite="lax",
                max_age=600,
                path="/",
            )
            response.headers["Cache-Control"] = "no-store"
            return response

        @self.router.get("/callback")
        async def callback(request: Request, state: str, code: str) -> RedirectResponse:
            tokens, return_path = await self.login.callback(
                state, request.cookies.get(LOGIN_COOKIE, ""), code
            )
            response = RedirectResponse(return_path, status_code=303)
            self._cookies(response, tokens)
            response.delete_cookie(LOGIN_COOKIE, secure=True, httponly=True, path="/")
            response.headers["Cache-Control"] = "no-store"
            response.headers["Referrer-Policy"] = "no-referrer"
            return response

        @self.router.get("/me")
        async def me(principal: Annotated[Principal, Depends(self.principal)]) -> Principal:
            return principal

        @self.router.post("/refresh")
        async def refresh(body: CredentialRequest) -> dict[str, str]:
            return (await self.sessions.refresh(body.credential.get_secret_value())).wire()

        @self.router.post("/browser-refresh", status_code=204)
        async def browser_refresh(request: Request, response: Response) -> None:
            self.check_origin(request)
            tokens = await self.sessions.refresh(request.cookies.get(REFRESH_COOKIE, ""))
            self._cookies(response, tokens)

        @self.router.post("/logout", status_code=204)
        async def logout(
            request: Request,
            response: Response,
            principal: Annotated[Principal, Depends(self.principal)],
        ) -> None:
            token = request.headers.get("authorization", "").partition(" ")[2]
            token = token or request.cookies.get(SESSION_COOKIE, "")
            await asyncio.to_thread(self.sessions.revoke, token)
            response.delete_cookie(SESSION_COOKIE, path="/", secure=True, httponly=True)
            response.delete_cookie(REFRESH_COOKIE, path="/api/auth", secure=True, httponly=True)

        @self.router.post("/terminal")
        async def terminal() -> dict[str, str | int]:
            login = await asyncio.to_thread(self.login.terminal)
            return {
                "device_secret": login.device_secret,
                "user_code": login.user_code,
                "verification_uri": self.public_origin + "/api/auth/terminal/verify",
                "expires_in": login.expires_in,
                "interval": login.interval,
            }

        @self.router.post("/terminal/poll")
        async def poll(body: CredentialRequest) -> dict[str, str]:
            return (await self.login.poll(body.credential.get_secret_value())).wire()

        @self.router.post("/terminal/approve", status_code=204)
        async def approve(
            body: ApprovalRequest, principal: Annotated[Principal, Depends(self.principal)]
        ) -> None:
            await asyncio.to_thread(
                self.login.approve, body.user_code, Identity(principal.subject, principal.email)
            )

        @self.router.get("/terminal/verify", response_class=HTMLResponse)
        async def verify() -> str:
            return TERMINAL_PAGE


TERMINAL_PAGE = """<!doctype html><html lang="en"><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Fletcherlake terminal login</title><main>
<h1>Approve your terminal</h1><p>
<a href="/api/auth/login?return_to=/api/auth/terminal/verify">Sign in with Google</a>,
then enter the code displayed by fl-client on your own terminal.</p>
<form id="approval"><label>Terminal code <input name="user_code" required maxlength="32"
autocomplete="off"></label><button>Approve terminal</button></form><p id="result" role="status"></p>
</main><script>
document.getElementById('approval').addEventListener('submit', async event => {
  event.preventDefault();
  const response = await fetch('/api/auth/terminal/approve', {
    method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({user_code: new FormData(event.target).get('user_code')})
  });
  document.getElementById('result').textContent = response.ok ?
    'Terminal approved. Return to fl-client.' : 'Approval failed. Sign in and check your code.';
});
</script></html>"""
