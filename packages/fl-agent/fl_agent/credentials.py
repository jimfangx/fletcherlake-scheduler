"""Protected scheduler credentials live separately from public cluster inventory."""

import json
import os
from pathlib import Path

from fl_common.models.base import Schema
from pydantic import SecretStr


class AgentCredentials(Schema):
    scheduler_url: str
    agent_token: SecretStr
    enrollment_url: str | None = None

    def protected_bytes(self) -> bytes:
        body = self.model_dump(mode="json")
        body["agent_token"] = self.agent_token.get_secret_value()
        return json.dumps(body).encode()


def load_credentials(path: Path) -> AgentCredentials | None:
    if not path.exists():
        return None
    stat = path.stat()
    if stat.st_mode & 0o077 or stat.st_uid != os.geteuid():
        raise PermissionError(
            "Agent credential file must be owned by the daemon user and mode 0600"
        )
    return AgentCredentials.model_validate_json(path.read_text())
