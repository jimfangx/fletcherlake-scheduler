"""OS-held locks survive neither process death nor reboot; their files may remain."""

import fcntl
import os
from pathlib import Path
from typing import IO

from .errors import PlatformError


class ExclusiveLock:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._file: IO[str] | None = None

    def acquire(self) -> None:
        if self.held:
            raise PlatformError("ALREADY_OWNED", f"Lock is already held: {self.path}")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(self.path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        handle = os.fdopen(descriptor, "a+")
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            handle.close()
            raise PlatformError("ALREADY_OWNED", f"Lock is held: {self.path}") from error
        except OSError:
            handle.close()
            raise
        self._file = handle

    def release(self) -> None:
        if self._file is not None:
            self._file.close()
            self._file = None

    def __enter__(self) -> "ExclusiveLock":
        self.acquire()
        return self

    def __exit__(self, *args: object) -> None:
        self.release()

    @property
    def held(self) -> bool:
        return self._file is not None
