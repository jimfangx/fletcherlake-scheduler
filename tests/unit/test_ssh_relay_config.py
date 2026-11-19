"""Existing-host SSH configuration updates preserve policy and roll back failed checks."""

import json
import subprocess
from pathlib import Path

import pytest

from ship.cloud import ssh_relay_config

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def installed(tmp_path):
    template = (ROOT / "services/scheduler/nginx.conf").read_text()
    nginx = tmp_path / "scheduler.conf"
    nginx.write_text(ssh_relay_config.SSH_LOCATION.sub("", template) + "\n# custom setting\n")
    policy = tmp_path / "policy.json"
    policy.write_text(
        json.dumps(
            {
                "tagOwners": {"tag:custom": ["tag:owner"]},
                "acls": [
                    {
                        "action": "accept",
                        "proto": "tcp",
                        "src": ["tag:custom"],
                        "dst": ["tag:custom:443"],
                    }
                ],
                "ssh": [],
            }
        )
    )
    nginx.chmod(0o640)
    policy.chmod(0o600)
    return nginx, policy, tmp_path / "backups"


def test_existing_configuration_upgrade_preserves_other_access_and_is_idempotent(
    installed, monkeypatch
):
    nginx, policy, backups = installed
    old = {path: path.read_bytes() for path in (nginx, policy)}
    calls = []
    monkeypatch.setattr(ssh_relay_config, "run", lambda *argv: calls.append(argv))
    ssh_relay_config.install(ROOT, nginx, policy, backups)
    assert "location /api/ssh/" in nginx.read_text()
    assert nginx.read_text().endswith("# custom setting\n")
    assert nginx.stat().st_mode & 0o777 == 0o640
    assert policy.stat().st_mode & 0o777 == 0o600
    rules = json.loads(policy.read_text())
    assert rules["tagOwners"] == {"tag:custom": ["tag:owner"]}
    assert rules["acls"][0] == json.loads(old[policy])["acls"][0]
    assert rules["acls"][1] == {
        "action": "accept",
        "proto": "tcp",
        "src": ["tag:scheduler"],
        "dst": ["tag:cluster:22"],
    }
    backup = next(backups.iterdir())
    assert (backup / nginx.name).read_bytes() == old[nginx]
    assert (backup / policy.name).read_bytes() == old[policy]
    assert calls[0] == ("nginx", "-t")
    assert calls[1][0] == "headscale"
    assert calls[-2:] == [
        ("systemctl", "kill", "--kill-whom=main", "--signal=HUP", "headscale"),
        ("systemctl", "reload", "nginx"),
    ]
    current = {path: path.read_bytes() for path in (nginx, policy)}
    ssh_relay_config.install(ROOT, nginx, policy, backups)
    assert {path: path.read_bytes() for path in (nginx, policy)} == current
    assert len(list(backups.iterdir())) == 1


def test_invalid_configuration_restores_both_files_without_reloading(installed, monkeypatch):
    nginx, policy, backups = installed
    old = {path: path.read_bytes() for path in (nginx, policy)}
    calls = []

    def fail(*argv):
        calls.append(argv)
        raise subprocess.CalledProcessError(1, argv)

    monkeypatch.setattr(ssh_relay_config, "run", fail)
    with pytest.raises(subprocess.CalledProcessError):
        ssh_relay_config.install(ROOT, nginx, policy, backups)
    assert {path: path.read_bytes() for path in (nginx, policy)} == old
    assert calls == [("nginx", "-t")]


def test_websocket_rejection_reports_status_without_response_secrets(monkeypatch, capsys):
    from fl_client import ssh
    from websockets.datastructures import Headers
    from websockets.exceptions import InvalidStatus
    from websockets.http11 import Response

    def rejected(path):
        raise InvalidStatus(Response(404, "Not Found", Headers(), b"private ticket body"))

    monkeypatch.setattr(ssh, "proxy", rejected)
    monkeypatch.setattr("sys.argv", ["ssh-proxy", "ticket.json"])
    assert ssh.main() == 1
    message = capsys.readouterr().err
    assert "HTTP 404" in message and "nginx" in message
    assert "private ticket body" not in message
