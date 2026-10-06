"""Lifecycle safety contracts with native effects injected, preserving Mac target behavior."""

import json
from uuid import uuid4

import httpx
import pytest
import yaml
from fl import Cluster, ClusterSetup
from fl.macos.inventory import inventory
from fl.macos.provisioning import LABEL, launchd_plist
from fl_agent.configuration import config_confirmed, confirm_config, load_config, write_config
from fl_common.errors import PlatformError
from fl_common.files import atomic_write


@pytest.fixture
def management(tmp_path, config):
    config.cluster_id = uuid4()
    state = tmp_path / "state"
    write_config(state, config)
    confirm_config(state)
    (tmp_path / "pixi.toml").write_text("placeholder")
    operations, clients = [], []

    def rpc(request):
        operations.append((request.method, request.url.path, request.content))
        if request.url.path.endswith("/drain"):
            assert not config_confirmed(state) or b"RECONFIGURING" not in request.content
            return httpx.Response(200, json={"state": json.loads(request.content)["state"]})
        return httpx.Response(200, json={"state": "READY"})

    def local(socket):
        assert socket == tmp_path / "run" / "agent.sock"
        client = httpx.Client(transport=httpx.MockTransport(rpc), base_url="http://fl-agent")
        clients.append(client)
        return Cluster(client)

    setup = ClusterSetup(
        state,
        tmp_path / "run",
        authorize=lambda: None,
        runner=operations.append,
        launchd_root=tmp_path / "launchd",
        pixi="/opt/pixi/bin/pixi",
        client_factory=local,
        probe=lambda _: True,
    )
    atomic_write(
        setup.launchd_root / f"{LABEL}.plist",
        launchd_plist(tmp_path, state, setup.runtime_root, "/opt/pixi/bin/pixi"),
        mode=0o644,
    )
    return setup, operations, clients


def test_confirm_waits_for_local_ready_after_native_install(management, tmp_path):
    setup, operations, clients = management
    (setup.launchd_root / f"{LABEL}.plist").unlink()
    assert setup.confirm(tmp_path) == {"state": "READY"}
    assert operations[0][1:3] == ["install", "--locked"]
    assert operations[1][1:3] == ["bootstrap", "system"]
    assert operations[2][1] == "kickstart"
    assert operations[3][1] == "/v1/status"
    assert config_confirmed(setup.state_root) and all(client.is_closed for client in clients)
    assert (setup.launchd_root / f"{LABEL}.plist").is_file()


def test_reconfigure_invalidates_before_edit_and_preserves_identity(management, config, tmp_path):
    setup, operations, clients = management
    original = load_config(setup.state_root).cluster_id
    config.cluster_id = None
    config.apple_model = "replacement model"

    def prepare():
        assert not config_confirmed(setup.state_root)
        assert json.loads(operations[0][2])["state"] == "RECONFIGURING"
        return config

    setup.reconfigure(tmp_path, config_factory=prepare)
    assert config.cluster_id is None  # The SDK owns its copy, not the caller's model.
    assert load_config(setup.state_root).cluster_id == original
    assert load_config(setup.state_root).apple_model == "replacement model"
    assert operations[2][1] == "bootout"
    assert config_confirmed(setup.state_root) and all(client.is_closed for client in clients)


@pytest.mark.parametrize("failure", ["parse", "editor", "identity", "sync"])
def test_reconfigure_failures_leave_confirmation_absent(management, tmp_path, failure):
    setup, operations, clients = management
    original = load_config(setup.state_root)
    malformed = tmp_path / "broken.yaml"
    malformed.write_text("boards: [broken")

    def editor(path):
        assert not config_confirmed(setup.state_root)
        assert path.stat().st_mode & 0o777 == 0o600
        raise RuntimeError("Editor failed")

    def fail_sync(argv):
        raise RuntimeError("Sync failed")

    replacement = original.model_copy(deep=True)
    if failure == "identity":
        replacement.cluster_id = uuid4()
    if failure == "sync":
        setup.runner = fail_sync
    expected = {
        "parse": yaml.parser.ParserError,
        "editor": RuntimeError,
        "identity": PlatformError,
        "sync": RuntimeError,
    }[failure]
    with pytest.raises(expected):
        setup.reconfigure(
            tmp_path,
            malformed if failure == "parse" else replacement,
            editor=editor if failure == "editor" else None,
        )
    assert not config_confirmed(setup.state_root)
    assert load_config(setup.state_root).cluster_id == original.cluster_id
    assert all(client.is_closed for client in clients)
    assert len(operations) == 1  # Drain completed, but no launchd or reboot request escaped.


def test_restart_waits_for_daemon_ack_before_reboot(management):
    setup, operations, clients = management
    setup.restart()
    assert json.loads(operations[0][2]) == {"state": "RESTARTING"}
    assert operations[1] == ["/sbin/shutdown", "-r", "now"]
    assert all(client.is_closed for client in clients)


def test_rejected_hardware_shutdown_blocks_reboot(management):
    setup, operations, clients = management
    client = httpx.Client(
        base_url="http://fl-agent",
        transport=httpx.MockTransport(
            lambda _: httpx.Response(409, json={"code": "SHUTDOWN_FAILED"})
        ),
    )
    setup.client_factory = lambda _: Cluster(client)
    with pytest.raises(httpx.HTTPStatusError):
        setup.restart()
    assert not operations and client.is_closed


@pytest.mark.parametrize(
    "system,euid,code", [("Linux", 0, "MACOS_REQUIRED"), ("Darwin", 501, "ROOT_REQUIRED")]
)
def test_library_requires_native_host_and_privilege_without_reexecution(
    monkeypatch, tmp_path, system, euid, code
):
    monkeypatch.setattr("fl.macos.provisioning.platform.system", lambda: system)
    monkeypatch.setattr("fl.macos.provisioning.os.geteuid", lambda: euid)
    calls = []
    setup = ClusterSetup(tmp_path / "missing", runner=calls.append)
    with pytest.raises(PlatformError) as error:
        setup.confirm(tmp_path)
    assert error.value.code == code
    assert not calls and not setup.state_root.exists()


def test_inventory_precedence_preserves_explicit_nulls(config):
    overrides = config.model_dump(mode="json")
    overrides["environment"] = {"gcc": None}
    detected = {"apple_model": "detected", "environment": {"gcc": {"path": "/usr/bin/gcc"}}}
    parsed = inventory(overrides, interactive={"apple_model": "prompt"}, detector=lambda: detected)
    assert parsed.apple_model == config.apple_model and parsed.environment.gcc is None
    overrides.pop("apple_model")
    assert (
        inventory(
            overrides, interactive={"apple_model": "prompt"}, detector=lambda: detected
        ).apple_model
        == "prompt"
    )


def test_cli_invalid_replacement_uses_shared_invalidation_and_drain(
    management, tmp_path, monkeypatch
):
    from fl_cli import setup as terminal

    setup, operations, clients = management
    monkeypatch.setattr(terminal, "require_macos", lambda: None)
    monkeypatch.setattr(terminal, "ensure_root", lambda: None)
    monkeypatch.setattr(terminal, "ClusterSetup", lambda *args: setup)
    malformed = tmp_path / "broken.yaml"
    malformed.write_text("boards: [broken")
    with pytest.raises(yaml.parser.ParserError):
        terminal.reconfigure(tmp_path, config=malformed)
    assert not config_confirmed(setup.state_root)
    assert json.loads(operations[0][2])["state"] == "RECONFIGURING"
    assert len(operations) == 1 and all(client.is_closed for client in clients)


def test_cli_interactive_reconfigure_opens_current_inventory_after_drain(
    management, tmp_path, monkeypatch
):
    import fl_cli.inventory_editor as editor
    import typer
    from fl_cli import setup as terminal

    setup, operations, clients = management
    original = load_config(setup.state_root)
    monkeypatch.setattr(terminal, "require_macos", lambda: None)
    monkeypatch.setattr(terminal, "ensure_root", lambda: None)
    monkeypatch.setattr(terminal, "ClusterSetup", lambda *args: setup)
    monkeypatch.setattr(terminal, "detect_host", lambda: {})

    def canceled(initial):
        assert not config_confirmed(setup.state_root)
        assert json.loads(operations[0][2])["state"] == "RECONFIGURING"
        assert initial["cluster_id"] == str(original.cluster_id)
        raise typer.Abort()

    monkeypatch.setattr(editor, "edit_inventory", canceled)
    with pytest.raises(typer.Abort):
        terminal.reconfigure(
            tmp_path,
            state_root=setup.state_root,
            runtime_root=setup.runtime_root,
            interactive=True,
        )
    assert load_config(setup.state_root) == original
    assert len(operations) == 1 and all(client.is_closed for client in clients)
