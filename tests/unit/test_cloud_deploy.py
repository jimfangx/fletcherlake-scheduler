import io
import json
import shlex
import subprocess
import tarfile
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from ship.cloud import bootstrap, deploy
from ship.cloud.package_release import build_release


def git(repo, *args):
    return subprocess.check_output(["git", "-C", str(repo), *args], text=True).strip()


def commit(repo):
    git(repo, "add", ".")
    git(repo, "-c", "user.name=Test", "-c", "user.email=test@example.test", "commit", "-m", "init")
    return git(repo, "rev-parse", "HEAD")


def test_release_includes_exact_commits_host_code_and_built_dashboard(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init")
    host = repo / "packages/lilikoi"
    (host / "host/lilikoi").mkdir(parents=True)
    (host / "host/lilikoi/__init__.py").write_text("# committed host\n")
    (host / "pico-sdk").mkdir()
    (host / "pico-sdk/firmware.txt").write_text("not runtime")
    git(host, "init")
    commit(host)
    (repo / "pixi.lock").write_text("committed lock")
    revision = commit(repo)
    (repo / "pixi.lock").write_text("uncommitted lock")
    (host / "host/lilikoi/__init__.py").write_text("# uncommitted host\n")
    (repo / "private-key").write_text("untracked secret")
    (repo / "services/dashboard/dist").mkdir(parents=True)
    (repo / "services/dashboard/dist/index.html").write_text("built UI")
    output = tmp_path / "cloud.tar.gz"
    build_release(repo, revision, output)
    with tarfile.open(output) as bundle:
        assert bundle.extractfile("pixi.lock").read() == b"committed lock"
        assert (
            bundle.extractfile("packages/lilikoi/host/lilikoi/__init__.py").read()
            == b"# committed host\n"
        )
        assert bundle.extractfile("services/dashboard/dist/index.html").read() == b"built UI"
        assert bundle.extractfile("DEPLOYED_REVISION").read().decode().strip() == revision
        assert "private-key" not in bundle.getnames()
        assert not any("pico-sdk" in member.name for member in bundle)
    with pytest.raises(ValueError, match="checked-out commit"):
        build_release(repo, "0" * 40, tmp_path / "wrong.tar.gz")


@pytest.fixture
def cloud_env(tmp_path, monkeypatch):
    values = {
        "GITHUB_RUN_ID": "123",
        "GITHUB_RUN_ATTEMPT": "1",
        "FL_GCP_PROJECT": "project",
        "FL_GCP_ZONE": "zone",
        "FL_SCHEDULER_VM": "scheduler-vm",
        "FL_GATEWAY_VM": "gateway-vm",
        "FL_DEPLOY_SSH_KEY": "private key",
        "FL_DEPLOY_KNOWN_HOSTS": "pinned hosts\n",
        "FL_DOMAIN": "example.test",
    }
    for name, value in values.items():
        monkeypatch.setenv(name, value)
    archive = tmp_path / "release.tar.gz"
    archive.write_bytes(b"release")
    return archive


@pytest.mark.parametrize("failure", [None, ("gateway", "prepare"), ("scheduler", "migrate")])
def test_deploy_orders_preparation_backups_migrations_and_health(cloud_env, monkeypatch, failure):
    phases = []
    keys = []

    def run(argv):
        if argv[0] in {"ssh", "scp"}:
            key = Path(argv[argv.index("-i") + 1])
            assert key.stat().st_mode & 0o777 == 0o600
            keys.append(key)
            assert "StrictHostKeyChecking=yes" in argv
            proxy = next(item for item in argv if item.startswith("ProxyCommand="))
            assert "start-iap-tunnel" in proxy
        if argv[0] == "ssh":
            command = shlex.split(argv[-1])
            phase, role = command[4:6]
            phases.append((role, phase))
            if (role, phase) == failure:
                raise subprocess.CalledProcessError(1, argv)

    monkeypatch.setattr(deploy, "run", run)
    if failure:
        with pytest.raises(subprocess.CalledProcessError):
            deploy.deploy(cloud_env, "a" * 40)
    else:
        deploy.deploy(cloud_env, "a" * 40)
    assert phases[:2] == [("scheduler", "prepare"), ("gateway", "prepare")]
    if failure == ("gateway", "prepare"):
        assert len(phases) == 2  # Installation failed before stopping either live service.
    else:
        assert phases[2:8] == [
            (role, phase)
            for phase in ("stop", "backup", "activate")
            for role in ("scheduler", "gateway")
        ]
        assert phases[8] == ("scheduler", "migrate")
        expected = (
            [("scheduler", "stop"), ("gateway", "stop")]
            if failure
            else [("gateway", "start"), ("scheduler", "start")]
        )
        assert phases[9:] == expected
    assert keys and all(not key.exists() for key in keys)


def test_workflow_only_deploys_tested_main_pushes_and_serializes_updates():
    repo = Path(__file__).resolve().parents[2]
    workflow = yaml.load((repo / ".github/workflows/ci.yml").read_text(), Loader=yaml.BaseLoader)
    job = workflow["jobs"]["deploy"]
    assert job["needs"] == "python"
    assert job["if"] == "github.event_name == 'push' && github.ref == 'refs/heads/main'"
    assert job["environment"] == "production"
    assert job["concurrency"]["cancel-in-progress"] == "false"
    assert job["permissions"]["id-token"] == "write"


def test_manual_bootstrap_creates_settings_without_github_auth_or_writes(tmp_path, monkeypatch):
    args = SimpleNamespace(
        manual_github=True,
        repo="owner/repo",
        project="project",
        zone="zone",
        scheduler="scheduler-vm",
        gateway="gateway-vm",
        state=tmp_path,
        admin_key=tmp_path / "admin-key",
        admin_user="ubuntu",
        domain="example.test",
    )
    commands = []

    def call(argv, **kwargs):
        commands.append(argv)
        assert argv[0] != "gh"
        if argv[0] == "ssh-keygen":
            key = Path(argv[argv.index("-f") + 1])
            key.write_text("test private key")
            key.with_suffix(".pub").write_text("ssh-ed25519 fake-public-key")
        return ""

    def cloud(*argv, **kwargs):
        commands.append(["gcloud", *argv])
        return "ssh-ed25519 fake-host-key\n" if argv[:2] == ("compute", "ssh") else ""

    def describe(*argv):
        if argv[:2] == ("projects", "describe"):
            return {"projectNumber": "12345"}
        if argv[:2] == ("compute", "project-info"):
            return {}
        return {
            "networkInterfaces": [
                {
                    "networkIP": "10.80.0.10" if "scheduler-vm" in argv else "10.80.0.11",
                    "network": "path/network",
                }
            ]
        }

    monkeypatch.setattr(bootstrap, "call", call)
    monkeypatch.setattr(bootstrap, "gcloud", cloud)
    monkeypatch.setattr(bootstrap, "describe", describe)
    monkeypatch.setattr(bootstrap, "exists", lambda argv: False)
    monkeypatch.setattr(bootstrap.shutil, "which", lambda name: None if name == "gh" else name)
    monkeypatch.setattr(
        bootstrap.urllib.request,
        "urlopen",
        lambda *args, **kwargs: io.BytesIO(
            json.dumps(
                {
                    "id": 555,
                    "owner": {"id": 444},
                    "default_branch": "main",
                }
            ).encode()
        ),
    )
    bootstrap.configure(args)
    values = json.loads((tmp_path / "github-production-variables.json").read_text())
    assert values["FL_WORKLOAD_IDENTITY_PROVIDER"] == (
        "projects/12345/locations/global/workloadIdentityPools/fl-github/providers/main"
    )
    assert len(values) == 8
    assert (tmp_path / "github-deploy-ed25519").stat().st_mode & 0o777 == 0o600
    assert "private key" not in json.dumps(values)
    provider = next(cmd for cmd in commands if "create-oidc" in cmd)
    condition = next(arg for arg in provider if arg.startswith("--attribute-condition="))
    for claim in (
        "repository_id == '555'",
        "repository_owner_id == '444'",
        "event_name == 'push'",
        "ref == 'refs/heads/main'",
        "owner/repo/.github/workflows/ci.yml@refs/heads/main",
    ):
        assert claim in condition
    assert not any("setMetadata" in str(cmd) or "keys" in cmd for cmd in commands)
