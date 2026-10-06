from pathlib import Path

import pytest
from fl_agent.connection import websocket_url
from fl_agent.credentials import load_credentials
from fl_common.models.scheduler import Principal, Role


def test_production_connection_requires_tls_and_has_no_embedded_credentials() -> None:
    assert (
        websocket_url("https://scheduler.example.edu", "cluster")
        == "wss://scheduler.example.edu/api/agents/cluster/ws"
    )
    for url in (
        "http://scheduler.example.edu",
        "https://secret@scheduler.example.edu",
        "https://scheduler.example.edu?token=secret",
    ):
        with pytest.raises(ValueError):
            websocket_url(url, "cluster")
    assert websocket_url("http://localhost", "cluster", allow_http=True).startswith("ws://")


def test_credentials_are_separate_protected_and_redacted(tmp_path: Path) -> None:
    path = tmp_path / "credentials.json"
    path.write_text('{"scheduler_url":"https://scheduler.example.edu","agent_token":"secret"}')
    path.chmod(0o644)
    with pytest.raises(PermissionError):
        load_credentials(path)
    path.chmod(0o600)
    credentials = load_credentials(path)
    assert credentials.agent_token.get_secret_value() == "secret"
    assert "secret" not in repr(credentials)
    assert load_credentials(tmp_path / "absent.json") is None


def test_roles_include_force_off_scaffold_but_never_power_on() -> None:
    for role in Role:
        principal = Principal(email="alice@example.edu", subject="alice", role=role)
        assert principal.permits("job:submit")
        assert not principal.permits("cluster:poweron")
        assert principal.permits("cluster:force_poweroff") == (role != Role.USER)
