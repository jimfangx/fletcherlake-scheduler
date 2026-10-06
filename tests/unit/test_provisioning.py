import plistlib
from pathlib import Path

import pytest
from fl_agent.configuration import config_confirmed, confirm_config, write_config
from fl_agent.detection import merge_overrides
from fl_cli.provisioning import LABEL, install_agent, launchd_plist


def test_explicit_values_override_nested_autodetection() -> None:
    detected = {
        "environment": {"gcc": {"path": "/detected/gcc", "version": "12"}},
        "apple_model": "detected",
    }
    explicit = {"environment": {"gcc": {"path": "/custom/gcc"}}, "apple_model": "manual"}
    merged = merge_overrides(detected, explicit)
    assert merged["environment"]["gcc"] == {"path": "/custom/gcc", "version": "12"}
    assert merged["apple_model"] == "manual"
    assert detected["apple_model"] == "detected"


def test_configuration_marker_matches_exact_bytes(tmp_path: Path, config) -> None:
    write_config(tmp_path, config)
    assert not config_confirmed(tmp_path)
    confirm_config(tmp_path)
    assert config_confirmed(tmp_path)
    assert (tmp_path / "cluster.yaml").stat().st_mode & 0o777 == 0o600
    with (tmp_path / "cluster.yaml").open("a") as handle:
        handle.write("\n")
    assert not config_confirmed(tmp_path)


def test_macos_launchd_contract(tmp_path: Path) -> None:
    project = tmp_path / "project with spaces"
    state = Path("/Library/Application Support/fl")
    document = plistlib.loads(
        launchd_plist(project, state, Path("/var/run/fl"), "/opt/pixi/bin/pixi")
    )
    assert document["Label"] == LABEL
    argv = document["ProgramArguments"]
    assert argv[argv.index("--manifest-path") + 1] == str(project / "pixi.toml")
    assert str(state) in document["ProgramArguments"]
    assert document["KeepAlive"] and document["RunAtLoad"]
    assert document["Umask"] == 0o077
    assert "/opt/homebrew/bin" in document["EnvironmentVariables"]["PATH"].split(":")


def test_failed_sync_never_confirms_config(tmp_path: Path, config) -> None:
    state = tmp_path / "state"
    write_config(state, config)
    confirm_config(state)
    (tmp_path / "pixi.toml").write_text("placeholder")

    def fail(argv: list[str]) -> None:
        raise RuntimeError("sync failed")

    with pytest.raises(RuntimeError, match="sync failed"):
        install_agent(tmp_path, state, tmp_path / "run", runner=fail, pixi="pixi")
    assert not config_confirmed(state)


def test_confirm_installs_then_starts_launchd(tmp_path: Path, config) -> None:
    state = tmp_path / "state"
    write_config(state, config)
    (tmp_path / "pixi.toml").write_text("placeholder")
    calls: list[list[str]] = []
    install_agent(
        tmp_path,
        state,
        tmp_path / "run",
        runner=calls.append,
        launchd_root=tmp_path / "launchd",
        pixi="/opt/bin/pixi",
    )
    assert calls[0][1] == "install"
    assert calls[1][1:3] == ["bootstrap", "system"]
    assert calls[2][1] == "kickstart"
    assert config_confirmed(state)
