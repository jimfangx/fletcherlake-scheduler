"""Only successful programming populates an ephemeral, atomically replaced cache."""

from pathlib import Path

from fl_common.models.board import Identifier
from pydantic import TypeAdapter

from .files import atomic_write


class BitstreamCache:
    def __init__(self, root: Path) -> None:
        self.root = root
        root.mkdir(parents=True, exist_ok=True)

    def clear(self) -> None:
        for path in self.root.glob("*.bitstream.sha256"):
            path.unlink()

    def _path(self, board_id: str) -> Path:
        TypeAdapter(Identifier).validate_python(board_id)
        return self.root / f"{board_id}.bitstream.sha256"

    def matches(self, board_id: str, sha256: str) -> bool:
        path = self._path(board_id)
        return path.exists() and path.read_text().strip() == sha256

    def record(self, board_id: str, sha256: str) -> None:
        atomic_write(self._path(board_id), (sha256 + "\n").encode())

    def invalidate(self, board_id: str) -> None:
        self._path(board_id).unlink(missing_ok=True)
