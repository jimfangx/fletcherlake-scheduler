"""Exercise packaging and SSH orchestration without connecting to a deployment."""

import json
import os
import shutil
import subprocess
import sys
import tarfile
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "ship/18-bringup-mac.sh"


@pytest.fixture
def operator(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()

    def git(directory, *args):
        subprocess.run(["git", "-C", str(directory), *args], check=True, capture_output=True)

    git(repo, "init")
    (repo / "ship").mkdir()
    shutil.copyfile(SCRIPT, repo / "ship/18-bringup-mac.sh")
    for name in ("19-2-update-existing-mac.sh", "mac-update.py", "setup-mac-cli-shell.sh"):
        shutil.copyfile(SCRIPT.parent / name, repo / "ship" / name)
    for name in ("pixi.toml", "pixi.lock", "pyproject.toml"):
        (repo / name).write_text("original\n")
    (repo / ".gitignore").write_text(".pixi/\n")
    lilikoi = repo / "packages/lilikoi"
    (lilikoi / "host/lilikoi").mkdir(parents=True)
    (lilikoi / "host/lilikoi/__init__.py").write_text("# host module\n")
    (lilikoi / "pico-sdk").mkdir()
    (lilikoi / "pico-sdk/large-sdk-file").write_text("firmware only\n")
    git(lilikoi, "init")
    git(lilikoi, "add", ".")
    git(
        lilikoi,
        "-c",
        "user.name=Test",
        "-c",
        "user.email=test@example.test",
        "commit",
        "-m",
        "init",
    )
    git(repo, "add", ".")
    (repo / "pixi.lock").write_text("current dirty lock\n")
    (repo / "new-backend.py").write_text("# new working-tree source\n")
    (repo / ".pixi").mkdir()
    (repo / ".pixi/ignored").write_text("environment\n")
    state = repo / "deploy-state"
    state.mkdir()
    (state / "secret.env").write_text("private deployment settings\n")
    config = tmp_path / "board overrides.yaml"
    config.write_text("boards: [null]\n")
    log = tmp_path / "calls.jsonl"
    remote_script = tmp_path / "remote-setup.sh"
    binaries = tmp_path / "bin"
    binaries.mkdir()
    stub = (
        f"#!{sys.executable}\n"
        + """\
import json, os, pathlib, shutil, sys
name = pathlib.Path(sys.argv[0]).name
with open(os.environ["MAC_TEST_LOG"], "a") as log:
    log.write(json.dumps([name, *sys.argv[1:]]) + "\\n")
if name == "ssh" and "mktemp" in sys.argv[-1]:
    kind = "update" if "fl-mac-update" in sys.argv[-1] else "setup"
    print(f"/tmp/fl-mac-{kind}.ABC123")
if name == "scp" and sys.argv[-1].endswith(("/setup.sh", "/update.sh")):
    shutil.copyfile(sys.argv[1], os.environ["MAC_TEST_REMOTE_SCRIPT"])
if name == "scp" and os.environ.get("MAC_TEST_FAIL_COPY"):
    sys.exit(7)
if name == "ssh" and "-t" in sys.argv and os.environ.get("MAC_TEST_FAIL_ACTIVATION"):
    sys.exit(9)
"""
    )
    for name in ("ssh", "scp", "curl"):
        executable = binaries / name
        executable.write_text(stub)
        executable.chmod(0o755)
    env = dict(os.environ)
    env.update(
        PATH=f"{binaries}:{env['PATH']}",
        FL_DOMAIN="scheduler-test.example",
        FL_STATE=str(state),
        MAC_TEST_LOG=str(log),
        MAC_TEST_REMOTE_SCRIPT=str(remote_script),
    )
    return repo, state, config, log, remote_script, env


def test_current_archive_and_ssh_config_alias_are_used(operator):
    repo, state, config, log, remote_script, env = operator
    result = subprocess.run(
        ["bash", str(repo / "ship/18-bringup-mac.sh"), "mac-alias", str(config)],
        env=env,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    with tarfile.open(state / "mac-source.tar") as archive:
        names = archive.getnames()
        assert archive.extractfile("pixi.lock").read() == b"current dirty lock\n"
        assert "new-backend.py" in names
        assert "packages/lilikoi/host/lilikoi/__init__.py" in names
        assert not any("pico-sdk" in p or ".git/" in p or ".pixi/" in p for p in names)
        assert not any(p.startswith("deploy-state/") for p in names)
    calls = [json.loads(line) for line in log.read_text().splitlines()]
    ssh = [call for call in calls if call[0] == "ssh"]
    assert ssh == [
        ["ssh", "mac-alias", "umask 077; mktemp -d /tmp/fl-mac-setup.XXXXXX"],
        ["ssh", "-t", "mac-alias", "bash '/tmp/fl-mac-setup.ABC123/setup.sh'"],
    ]
    copies = [call for call in calls if call[0] == "scp"]
    assert len(copies) == 3
    assert all(len(call) == 3 and call[2].startswith("mac-alias:") for call in copies)
    assert copies[-1][1] == str(config)
    subprocess.run(["bash", "-n", str(remote_script)], check=True)
    assert not list(state.glob("mac-setup.*"))


def test_failed_upload_prevents_remote_installation(operator):
    repo, state, config, log, _, env = operator
    env["MAC_TEST_FAIL_COPY"] = "1"
    result = subprocess.run(
        ["bash", str(repo / "ship/18-bringup-mac.sh"), "mac-alias", str(config)],
        env=env,
        capture_output=True,
    )
    assert result.returncode == 7
    calls = [json.loads(line) for line in log.read_text().splitlines()]
    assert not any(call[:2] == ["ssh", "-t"] for call in calls)
    assert not list(state.glob("mac-setup.*"))


def test_no_config_file_is_required_for_interactive_setup(operator):
    repo, _, _, log, remote_script, env = operator
    env.pop("FL_MAC_CONFIG", None)
    result = subprocess.run(
        ["bash", str(repo / "ship/18-bringup-mac.sh"), "mac-alias"],
        env=env,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    script = remote_script.read_text()
    assert "FL_CONFIG_SUPPLIED=0" in script
    assert '"$FL_PYTHON" -m fl_cli.mac_inventory "$FL_CONFIG"' in script
    calls = [json.loads(line) for line in log.read_text().splitlines()]
    assert len([call for call in calls if call[0] == "scp"]) == 2
    subprocess.run(["bash", "-n", str(remote_script)], check=True)
