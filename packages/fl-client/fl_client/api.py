"""HTTPS scheduler client with locked, one-use refresh-token rotation."""

from datetime import timedelta
from typing import Any
from uuid import UUID

import httpx
from fl_common.errors import PlatformError
from fl_common.models.base import utcnow

from .credentials import Credentials, CredentialStore


def checked(response: httpx.Response) -> Any:
    if response.is_redirect:
        raise PlatformError("HTTP_REDIRECT", "Scheduler API redirects are not accepted")
    if response.is_error:
        try:
            body = response.json()
            code = body.get("code", "HTTP_ERROR")
            message = body.get("message", "Scheduler rejected the request")
        except (ValueError, AttributeError):
            code, message = "HTTP_ERROR", "Scheduler rejected the request"
        raise PlatformError(str(code), str(message), status=response.status_code)
    return response.json() if response.content else None


class RemoteClient:
    def __init__(
        self, store: CredentialStore | None = None, *, transport: httpx.BaseTransport | None = None
    ) -> None:
        self.store = store or CredentialStore()
        self.http = httpx.Client(timeout=30, follow_redirects=False, transport=transport)

    def close(self) -> None:
        self.http.close()

    def __enter__(self) -> "RemoteClient":
        return self

    def __exit__(self, *args: object) -> None:
        self.close()

    def _credentials(self, stale_access: str | None = None) -> Credentials:
        with self.store.locked():
            try:
                credentials = self.store.load()
            except FileNotFoundError as error:
                raise PlatformError("LOGIN_REQUIRED", "Run fl-client login first") from error
            current = credentials.access_token.get_secret_value()
            force = stale_access is not None and current == stale_access
            if credentials.access_expires_at <= utcnow() + timedelta(seconds=30) or force:
                if credentials.refresh_expires_at <= utcnow():
                    raise PlatformError("LOGIN_REQUIRED", "Session expired; run fl-client login")
                body = checked(
                    self.http.post(
                        credentials.scheduler + "/api/auth/refresh",
                        json={"credential": credentials.refresh_token.get_secret_value()},
                    )
                )
                credentials = Credentials.from_response(credentials.scheduler, body)
                self.store.save(credentials)
            return credentials

    def request(self, method: str, path: str, **kwargs: Any) -> Any:
        if not path.startswith("/api/"):
            raise ValueError("Only scheduler API paths are supported")
        credentials = self._credentials()
        access = credentials.access_token.get_secret_value()
        response = self.http.request(
            method,
            credentials.scheduler + path,
            headers={"Authorization": "Bearer " + access},
            **kwargs,
        )
        if response.status_code == 401:
            credentials = self._credentials(stale_access=access)
            response = self.http.request(
                method,
                credentials.scheduler + path,
                headers={"Authorization": "Bearer " + credentials.access_token.get_secret_value()},
                **kwargs,
            )
        return checked(response)

    def jobs(self, limit: int = 100, offset: int = 0) -> list[dict[str, Any]]:
        return list(self.request("GET", "/api/jobs", params={"limit": limit, "offset": offset}))

    def origin(self) -> str:
        """Current authenticated scheduler destination, without exposing session credentials."""
        return self._credentials().scheduler

    def status(self, job_id: UUID) -> dict[str, Any]:
        return dict(self.request("GET", f"/api/jobs/{job_id}"))

    def cancel(self, job_id: UUID) -> None:
        self.request("POST", f"/api/jobs/{job_id}/cancel")

    def artifacts(self, job_id: UUID) -> list[dict[str, Any]]:
        return list(self.request("GET", f"/api/jobs/{job_id}/artifacts"))

    def logout(self) -> None:
        self.request("POST", "/api/auth/logout")
        with self.store.locked():
            self.store.delete()
