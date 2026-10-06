"""Interactive setup seeds overrides and reconfigure opens only after daemon drain."""

from copy import deepcopy

import pytest
import typer
from fl_cli import setup as terminal
from fl_cli.inventory_editor import InventoryEditor


def test_interactive_parse_seeds_overrides_and_returns_operator_edits(
    config, tmp_path, monkeypatch
):
    import fl_cli.inventory_editor as editor

    path = tmp_path / "inventory.yaml"
    path.write_text(config.model_dump_json())
    detected = {"apple_model": "detected model", "environment": {"chipyard": "/detected/chipyard"}}
    received = []

    def edit(initial):
        received.append(deepcopy(initial))
        result = config.model_copy(deep=True)
        result.apple_model = "operator changed model"
        return result

    monkeypatch.setattr(terminal, "detect_host", lambda: detected)
    monkeypatch.setattr(editor, "edit_inventory", edit)
    result = terminal.parse_config(path, interactive=True)
    assert received[0]["apple_model"] == config.apple_model
    assert received[0]["environment"]["chipyard"] is None  # Explicit null overrides detection.
    assert (
        result.apple_model == "operator changed model"
        and detected["apple_model"] == "detected model"
    )


def test_canceled_editor_does_not_return_configuration(config, monkeypatch):
    from fl_cli.inventory_editor import edit_inventory

    monkeypatch.setattr(InventoryEditor, "run", lambda _: None)
    with pytest.raises(typer.Abort):
        edit_inventory(config.model_dump(mode="json"))


def test_interactive_init_without_file_starts_only_disconnected_slots(config, monkeypatch):
    import fl_cli.inventory_editor as editor

    received = []
    monkeypatch.setattr(terminal, "detect_host", lambda: {"apple_model": "Detected Mac"})

    def edited(initial):
        received.append(deepcopy(initial))
        return config

    monkeypatch.setattr(editor, "edit_inventory", edited)
    assert terminal.parse_config(None, interactive=True) == config
    assert received == [{"apple_model": "Detected Mac", "boards": [None, None, None]}]
    with pytest.raises(typer.BadParameter, match="inventory file"):
        terminal.parse_config(None)


def test_init_cli_accepts_guided_setup_without_a_positional_file(config, monkeypatch):
    from types import SimpleNamespace

    import fl_cli.inventory_editor as editor
    from typer.testing import CliRunner

    calls = []
    monkeypatch.setattr(terminal, "require_macos", lambda: None)
    monkeypatch.setattr(terminal, "ensure_root", lambda: None)
    monkeypatch.setattr(terminal, "detect_host", lambda: {})
    monkeypatch.setattr(editor, "edit_inventory", lambda _: config)
    setup = SimpleNamespace(init=lambda *args, **kwargs: calls.append((args, kwargs)))
    monkeypatch.setattr(terminal, "ClusterSetup", lambda *args: setup)
    result = CliRunner().invoke(
        terminal.app,
        [
            "init",
            "--interactive",
            "--scheduler",
            "https://scheduler.test",
            "--enrollment-token",
            "ticket",
        ],
    )
    assert result.exit_code == 0, result.output
    assert calls == [
        ((config,), {"scheduler": "https://scheduler.test", "enrollment_token": "ticket"})
    ]
