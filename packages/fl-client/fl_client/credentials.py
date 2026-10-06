"""Per-user terminal credentials: protected files and serialized refresh rotation."""

import fcntl
import json
import os
import stat
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from fl_common.files import atomic_write, fsync_directory
from fl_common.models.base import Schema
from fl_common.network import https_origin
from pydantic import AwareDatetime, SecretStr, field_validator

__all__ = ["Credentials", "CredentialStore", "credential_path", "https_origin"]


class Credentials(Schema):
    scheduler: str
    access_token: SecretStr
    refresh_token: SecretStr
    access_expires_at: AwareDatetime
    refresh_expires_at: AwareDatetime

    @field_validator("scheduler")
    @classmethod
    def valid_origin(cls, value: str) -> str:
        return https_origin(value)

    @classmethod
    def from_response(cls, scheduler: str, body: dict[str, Any]) -> "Credentials":
        if body.get("token_type") != "Bearer":
            raise ValueError("Scheduler returned an unsupported credential type")
        keys = ("access_token", "refresh_token", "access_expires_at", "refresh_expires_at")
        return cls(scheduler=scheduler, **{key: body[key] for key in keys})

    def protected_bytes(self) -> bytes:
        body = self.model_dump(mode="json")
        body["access_token"] = self.access_token.get_secret_value()
        body["refresh_token"] = self.refresh_token.get_secret_value()
        return json.dumps(body).encode()


def credential_path() -> Path:
    root = Path(os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config")))
    return root / "fletcherlake" / "client.json"


class CredentialStore:
    def __init__(self, path: Path | None = None) -> None:
        self.path = path or credential_path()

    @staticmethod
    def _protected(info: os.stat_result, *, directory: bool = False) -> None:
        expected = stat.S_ISDIR if directory else stat.S_ISREG
        if not expected(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077:
            raise PermissionError("Credential storage must be owned by this user and private")

    @contextmanager
    def locked(self) -> Iterator[None]:
        self.path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
        self._protected(self.path.parent.lstat(), directory=True)
        descriptor = os.open(
            self.path.with_suffix(".lock"), os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600
        )
        try:
            self._protected(os.fstat(descriptor))
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            yield
        finally:
            os.close(descriptor)

    def load(self) -> Credentials:
        descriptor = os.open(self.path, os.O_RDONLY | os.O_NOFOLLOW)
        try:
            self._protected(os.fstat(descriptor))
            with os.fdopen(descriptor, "r", closefd=False) as handle:
                return Credentials.model_validate_json(handle.read())
        finally:
            os.close(descriptor)

    def save(self, credentials: Credentials) -> None:
        """Caller holds locked(); replacement is mode-0600 and fsynced."""
        atomic_write(self.path, credentials.protected_bytes())

    def delete(self) -> None:
        self.path.unlink(missing_ok=True)
        fsync_directory(self.path.parent)
