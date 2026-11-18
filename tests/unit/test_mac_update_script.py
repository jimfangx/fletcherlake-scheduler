"""Mac updater packaging, native activation ordering and failure boundaries."""

import json
import os
import runpy
import subprocess
import tarfile
from pathlib import Path

import pytest
from fl.macos.provisioning import LABEL, launchd_plist
from fl_agent.configuration import config_confirmed, load_config
from fl_common.errors import PlatformError

from tests.unit.test_lifecycle_sdk import management as management
from tests.unit.test_mac_bringup_script import operator as operator

SHIP = Path(__file__).resolve().parents[2] / "ship"
update = runpy.run_path(str(SHIP / "mac-update.py"))["update"]


@pytest.mark.parametrize("failure,expected", [(None, 0), ("copy", 7), ("activation", 9)])
def test_update_uploads_current_sources_and_cleans_staging(operator, failure, expected):
    repo, state, _, log, remote_script, env = operator
    env.pop("FL_DOMAIN")  # Existing enrollment supplies the scheduler; no domain/ticket needed.
    if failure:
        env[f"MAC_TEST_FAIL_{'COPY' if failure == 'copy' else 'ACTIVATION'}"] = "1"
    result = subprocess.run(
        ["bash", str(repo / "ship/19-2-update-existing-mac.sh"), "mac-alias"],
        env=env,
        capture_output=True,
        text=True,
    )
    assert result.returncode == expected, result.stderr
    with tarfile.open(state / "mac-update-source.tar") as archive:
        names = archive.getnames()
        assert archive.extractfile("pixi.lock").read() == b"current dirty lock\n"
        assert "new-backend.py" in names and "ship/mac-update.py" in names
        assert "packages/lilikoi/host/lilikoi/__init__.py" in names
        assert not any("pico-sdk" in p or ".git/" in p or ".pixi/" in p for p in names)
        assert not any(p.startswith("deploy-state/") for p in names)
    calls = [json.loads(line) for line in log.read_text().splitlines()]
    assert calls[-1] == ["ssh", "mac-alias", "rm -rf '/tmp/fl-mac-update.ABC123'"]
    assert any(call[:2] == ["ssh", "-t"] for call in calls) == (failure != "copy")
    assert not list(state.glob("mac-update.*"))
    if failure != "copy":
        subprocess.run(["bash", "-n", str(remote_script)], check=True)


@pytest.fixture
def prepared_update(management, tmp_path):
    setup, operations, _ = management
    project = tmp_path / "active"
    project.mkdir()
    (project / "original-source").write_text("original")
    (project / "pixi.toml").write_text("original manifest")
    release = tmp_path / "releases/new"
    release.mkdir(parents=True)
    (release / "pixi.toml").write_text("new manifest")
    plist = setup.launchd_root / f"{LABEL}.plist"
    plist.write_bytes(launchd_plist(project, setup.state_root, setup.runtime_root, "/bin/pixi"))
    (setup.state_root / "credentials.json").write_text('{"secret": "unchanged"}')
    (setup.state_root / "agent.db").write_bytes(b"queue stays in place")
    loaded = True

    def runner(argv):
        nonlocal loaded
        operations.append(argv)
        if argv[1] == "bootout":
            assert not config_confirmed(setup.state_root)
            assert project.resolve() != release  # Drain and bootout precede activation.
            assert (project / "original-source").is_file()
            loaded = False
        if argv[1] == "install":
            assert project.resolve() == release
        if argv[1] == "bootstrap":
            assert project.resolve() == release
            loaded = True

    setup.runner = runner
    setup.probe = lambda _: loaded
    return setup, operations, project, release


@pytest.mark.parametrize("symlink", [False, True])
def test_activation_preserves_identity_queue_credentials_and_prior_source(prepared_update, symlink):
    setup, operations, project, release = prepared_update
    if symlink:
        prior = project.with_name("old-release")
        project.rename(prior)
        project.symlink_to(prior, target_is_directory=True)
    original = load_config(setup.state_root)
    result = update(release, project, setup=setup)
    assert result == {"state": "READY"}
    assert project.is_symlink() and project.resolve() == release
    previous = Path((release / "previous-installation/previous-project").read_text().strip())
    assert (previous / "original-source").read_text() == "original"
    assert (setup.state_root / "agent.db").read_bytes() == b"queue stays in place"
    assert (setup.state_root / "credentials.json").read_text() == '{"secret": "unchanged"}'
    assert load_config(setup.state_root) == original
    assert config_confirmed(setup.state_root)
    assert (release / "previous-installation").stat().st_mode & 0o777 == 0o700
    assert json.loads(operations[0][2])["state"] == "RECONFIGURING"
    assert len([op for op in operations if isinstance(op, list) and op[1] == "bootout"]) == 1


def test_drain_failure_leaves_original_source_active_and_unconfirmed(prepared_update):
    setup, operations, project, release = prepared_update

    def fail_drain(_):
        raise PlatformError("SHUTDOWN_FAILED", "Board did not power down")

    setup._drain = fail_drain
    with pytest.raises(PlatformError, match="Board did not power down"):
        update(release, project, setup=setup)
    assert not project.is_symlink()
    assert (project / "original-source").exists()
    assert not (release / "previous-installation").exists()
    assert not config_confirmed(setup.state_root)
    assert not operations


def test_failed_switch_restores_legacy_directory(prepared_update, monkeypatch):
    setup, _, project, release = prepared_update

    def fail_replace(*args):
        raise OSError("Switch failed")

    monkeypatch.setattr(os, "replace", fail_replace)
    with pytest.raises(OSError, match="Switch failed"):
        update(release, project, setup=setup)
    assert (project / "original-source").read_text() == "original"
    assert not project.is_symlink()
    assert not config_confirmed(setup.state_root)
