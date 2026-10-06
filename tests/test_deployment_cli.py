"""Deployment CLI rejects misplaced credentials without echoing their values."""

import sys

import pytest
import yaml
from fl_deploy.main import main

from tests.deployment import inventory


@pytest.mark.parametrize("invalid_yaml", [False, True])
def test_invalid_inventory_does_not_echo_secrets(tmp_path, monkeypatch, capsys, invalid_yaml):
    path = tmp_path / "inventory.yaml"
    data = inventory(tmp_path)
    data["google_client_secret"] = "PRIVATE_DEPLOYMENT_MARKER"
    source = yaml.safe_dump(data)
    if invalid_yaml:
        source = "google_client_secret: [PRIVATE_DEPLOYMENT_MARKER\n"
    path.write_text(source)
    output = tmp_path / "bundle"
    monkeypatch.setattr(sys, "argv", ["fl-deploy", str(path), "--output", str(output)])
    with pytest.raises(SystemExit) as error:
        main()
    assert error.value.code == 2
    captured = capsys.readouterr()
    assert "PRIVATE_DEPLOYMENT_MARKER" not in captured.err + captured.out
    assert not output.exists()
    assert "Invalid deployment inventory" in captured.err or "valid YAML" in captured.err
