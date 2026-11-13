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


TERMINAL_PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Approve your terminal · Fletcherlake</title>
<style>
:root {
  font-family: Inter, ui-sans-serif, system-ui, -apple-system, sans-serif;
  color: #18343d;
  background: #f4f6f4;
  font-synthesis: none;
}
* { box-sizing: border-box; }
body { margin: 0; min-height: 100vh; min-height: 100dvh; }
main {
  min-height: 100vh;
  min-height: 100dvh;
  display: grid;
  place-items: center;
  padding: 32px 20px;
}
.card {
  width: 100%;
  max-width: 480px;
  padding: clamp(24px, 6vw, 40px);
  background: #fff;
  border: 1px solid #dce5e0;
  border-radius: 16px;
  box-shadow: 0 12px 40px rgb(21 45 53 / 6%);
}
.brand { display: flex; align-items: center; gap: 12px; font-weight: 650; }
.brand-mark {
  display: grid;
  place-items: center;
  width: 40px;
  height: 40px;
  border-radius: 11px;
  color: #d6eae5;
  background: #23454c;
  border: 1px solid #75aba5;
  font-weight: 700;
}
.eyebrow {
  margin: 32px 0 8px;
  color: #677f82;
  font-size: 0.72rem;
  font-weight: 700;
  letter-spacing: 0.12em;
  text-transform: uppercase;
}
h1 { margin: 0 0 12px; font-size: clamp(1.6rem, 6vw, 2rem); letter-spacing: -0.03em; }
p { margin: 0; line-height: 1.6; }
.description, .hint, .footer { color: #597176; }
.sign-in { margin: 24px 0; }
button, input, .button { font: inherit; }
button, .button {
  display: flex;
  align-items: center;
  justify-content: center;
  width: 100%;
  min-height: 46px;
  padding: 12px 16px;
  border: 1px solid #bccbc7;
  border-radius: 7px;
  background: #fff;
  color: #18343d;
  font-size: 0.92rem;
  font-weight: 600;
  text-decoration: none;
  cursor: pointer;
}
.button:hover { background: #e9f0ed; }
button { margin-top: 18px; background: #176f70; border-color: #176f70; color: #fff; }
button:hover { background: #0d5657; }
button:disabled { opacity: 0.65; cursor: default; }
:focus-visible { outline: 3px solid #9acac5; outline-offset: 3px; }
label { display: block; margin-bottom: 8px; font-size: 0.9rem; font-weight: 600; }
input {
  width: 100%;
  min-width: 0;
  padding: 14px 12px;
  border: 1px solid #bccbc7;
  border-radius: 7px;
  color: #18343d;
  background: #fff;
  font-family: ui-monospace, SFMono-Regular, Consolas, monospace;
  font-size: 1.1rem;
  letter-spacing: 0.06em;
  text-transform: uppercase;
}
input:disabled { background: #f4f6f4; }
.hint { margin-top: 8px; font-size: 0.8rem; }
code { font-family: ui-monospace, SFMono-Regular, Consolas, monospace; }
#result { font-size: 0.9rem; overflow-wrap: anywhere; }
#result:not(:empty) { margin-top: 20px; padding: 12px 14px; border-radius: 7px; }
#result[data-state="pending"] { background: #f0f4f2; color: #597176; }
#result[data-state="success"] { background: #e9f4ee; color: #245e42; }
#result[data-state="error"] { background: #fff0ec; color: #963c2f; }
.footer { margin-top: 24px; padding-top: 20px; border-top: 1px solid #e4ebe7; font-size: 0.8rem; }
.footer a { color: #126d70; text-decoration: none; }
.footer a:hover { text-decoration: underline; }
noscript p { margin-top: 16px; color: #963c2f; }
</style>
</head>
<body>
<main>
<section class="card" aria-labelledby="page-title">
<div class="brand"><span class="brand-mark" aria-hidden="true">FL</span>Fletcherlake</div>
<p class="eyebrow">Terminal access</p>
<h1 id="page-title">Approve your terminal</h1>
<p class="description">Sign in with Google, then enter the code shown by
<code>fl-client</code> on your terminal.</p>
<div class="sign-in">
<a class="button" href="/api/auth/login?return_to=/api/auth/terminal/verify">
Sign in with Google</a>
<p class="hint">Already signed in? Enter your code below.</p>
</div>
<form id="approval" method="post" action="/api/auth/terminal/approve">
<label for="user-code">Terminal code</label>
<input id="user-code" name="user_code" required maxlength="32" placeholder="XXXXXXX-XXXXXXX"
  autocomplete="off" autocapitalize="characters" spellcheck="false" aria-describedby="code-hint">
<p class="hint" id="code-hint">Codes expire after 10 minutes.
Only approve a code from your own terminal.</p>
<button id="approve-button" type="submit">Approve terminal</button>
</form>
<p id="result" role="status" aria-live="polite" aria-atomic="true"></p>
<noscript><p>Enable JavaScript to approve your terminal.</p></noscript>
<p class="footer">After approval, return to your terminal to continue.<br>
<a href="/">Back to dashboard</a></p>
</section>
</main>
<script>
const form = document.getElementById('approval');
const input = document.getElementById('user-code');
const button = document.getElementById('approve-button');
const result = document.getElementById('result');

function showResult(state, message) {
  result.dataset.state = state;
  result.textContent = message;
}

form.addEventListener('submit', async event => {
  event.preventDefault();
  if (button.disabled) return;
  const code = input.value.trim().toUpperCase();
  if (!code) {
    input.value = '';
    input.reportValidity();
    return;
  }
  input.value = code;
  button.disabled = true;
  button.textContent = 'Approving…';
  form.setAttribute('aria-busy', 'true');
  showResult('pending', 'Approving your terminal…');
  let approved = false;
  try {
    const response = await fetch('/api/auth/terminal/approve', {
      method: 'POST', credentials: 'same-origin', redirect: 'error',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({user_code: code})
    });
    if (response.ok) {
      approved = true;
      input.disabled = true;
      showResult('success', 'Terminal approved. Return to fl-client to continue.');
    } else {
      const error = await response.json().catch(() => ({}));
      let message = 'Approval failed. Check your code and try again.';
      if (response.status === 401) {
        message = 'Sign in with Google above, then enter your terminal code again.';
      } else if (response.status === 403) {
        message = 'Your account is not authorized. Use an account in a Fletcherlake group.';
      } else if (error.code === 'LOGIN_EXPIRED') {
        message = 'This code expired or was already used. Start a new login in fl-client.';
      } else if (response.status === 429) {
        message = 'Too many attempts. Wait a moment before trying again.';
      } else if (response.status >= 500) {
        message = 'Sign-in is temporarily unavailable. Please try again shortly.';
      }
      showResult('error', message);
    }
  } catch {
    showResult('error', 'Could not reach the scheduler. Check your connection and try again.');
  } finally {
    form.removeAttribute('aria-busy');
    button.disabled = approved;
    button.textContent = approved ? 'Terminal approved' : 'Approve terminal';
  }
});
</script>
</body>
</html>"""
