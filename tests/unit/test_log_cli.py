"""Redirected CLI log output preserves binary bytes and keeps approval text on stderr."""

from uuid import uuid4

from fl_client import main
from typer.testing import CliRunner


def test_logs_cli_writes_binary_stdout_and_separate_approval_messages(monkeypatch):
    payload = bytes(range(256)) + "🧪\n".encode()

    class Client:
        def __init__(self, store):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

    class Monitor:
        def __init__(self, client):
            pass

        def logs(self, job_id, **kwargs):
            assert kwargs == {"stream": "stderr", "offset": 5, "follow": True}
            yield payload[:257]
            yield payload[257:]

    monkeypatch.setattr(main, "RemoteClient", Client)
    monkeypatch.setattr(main, "Monitor", Monitor)
    monkeypatch.setattr(
        main, "ensure_login", lambda client, origin, display: display("Approve login")
    )
    result = CliRunner().invoke(
        main.app, ["logs", str(uuid4()), "--stream", "stderr", "--offset", "5", "--follow"]
    )
    assert result.exit_code == 0, result.exception
    assert result.stdout_bytes == payload and result.stderr_bytes == b"Approve login\n"
