"""Actual Textual forms require explicit boards and return only validated inventory."""

from copy import deepcopy

import pytest
from fl_cli.inventory_editor import InventoryEditor
from fl_cli.inventory_editor.board import BoardEditor
from textual.widgets import DataTable, Input, Select, Static, TabbedContent, TextArea


async def open_slot(app, pilot, slot):
    app.query_one(TabbedContent).active = "boards-tab"
    table = app.query_one("#board-slots", DataTable)
    table.move_cursor(row=slot)
    table.focus()
    await pilot.press("enter")
    await pilot.pause()
    assert isinstance(app.screen, BoardEditor)
    return app.screen


async def test_edit_boards_host_tools_and_power_without_mutating_source(config, tmp_path):
    initial = config.model_dump(mode="json")
    initial["boards"][2] = None
    original = deepcopy(initial)
    app = InventoryEditor(initial)
    async with app.run_test(size=(115, 42)) as pilot:
        board = await open_slot(app, pilot, 2)
        assert board.query_one("#board_id", Input).value == ""
        assert board.query_one("#backend", Select).value is Select.BLANK
        board.query_one("#board_id", Input).value = "new-real-board"
        await pilot.click("#save-board")
        assert "backend" in str(board.query_one("#board-error", Static).render())
        assert app.boards[2] is None
        await pilot.press("ctrl+s")
        assert isinstance(app.screen, BoardEditor) and app.return_value is None
        board.query_one("#backend", Select).value = "lilikoi"
        board.query_one("#num_vrails", Input).value = "2"
        board.query_one("#fpgas", TextArea).load_text("- model: VU9P\n  device: fpga0")
        board.query_one("#socs", TextArea).load_text("- name: chipB\n  device: soc0")
        board.query_one("#device_mapping", TextArea).load_text("uart: /dev/cu.board")
        board.query_one("#firmware_commands", TextArea).load_text(
            "stop: ['/opt/firmware tool', 'stop', '{uart}']"
        )
        await pilot.pause(0.25)  # Textual suppresses clicks during its button press animation.
        await pilot.click("#save-board")
        await pilot.pause()
        assert not isinstance(app.screen, BoardEditor), str(
            board.query_one("#board-error", Static).render()
        )
        assert app.boards[2]["backend"] == "lilikoi"
        # Cancellation rolls back an individual board's draft.
        board = await open_slot(app, pilot, 0)
        board.query_one("#board_id", Input).value = "discarded"
        await pilot.press("escape")
        assert app.boards[0]["board_id"] == "board-0"
        app.query_one("#apple_model", Input).value = "Macmini10,1"
        app.query_one("#environment", TextArea).load_text(
            "vivado: {path: /opt/vivado, version: '2026.1', edition: lab}"
        )
        app.query_one("#power_control", TextArea).load_text("type: kasa\nalias: Lab Mac")
        app.save_screenshot("inventory.svg", path=str(tmp_path))
        await pilot.press("ctrl+s")
    result = app.return_value
    assert result.apple_model == "Macmini10,1" and result.boards[2].num_vrails == 2
    assert result.boards[2].firmware_commands["stop"][0] == "/opt/firmware tool"
    assert result.environment.vivado.edition == "lab" and result.power_control.alias == "Lab Mac"
    assert initial == original


async def test_duplicate_ids_invalid_yaml_and_cancel_never_save(config):
    initial = config.model_dump(mode="json")
    app = InventoryEditor(initial)
    async with app.run_test(size=(115, 42)) as pilot:
        board = await open_slot(app, pilot, 1)
        board.query_one("#fpgas", TextArea).load_text("[broken")
        await pilot.click("#save-board")
        assert "Invalid YAML" in str(board.query_one("#board-error", Static).render())
        board.query_one("#fpgas", TextArea).load_text("[]")
        board.query_one("#board_id", Input).value = "board-0"
        await pilot.pause(0.25)
        await pilot.click("#save-board")
        await pilot.pause()
        assert not isinstance(app.screen, BoardEditor), str(
            board.query_one("#board-error", Static).render()
        )
        await pilot.press("ctrl+s")
        assert "unique" in str(app.query_one("#inventory-error", Static).render())
        assert app.return_value is None
        await pilot.click("#clear-slot")
        assert app.boards[1] is None
        app.query_one("#environment", TextArea).load_text("[not a mapping]")
        await pilot.press("ctrl+s")
        assert "environment" in str(app.query_one("#inventory-error", Static).render())
        await pilot.press("escape")
    assert app.return_value is None and initial == config.model_dump(mode="json")


@pytest.mark.parametrize("size", [(100, 35), (80, 24)])
async def test_invalid_existing_device_lists_can_be_repaired(config, size):
    initial = config.model_dump(mode="json")
    initial["boards"][0]["socs"] = "broken devices"
    initial["boards"][0]["clock_source"] = "unknown clock"
    app = InventoryEditor(initial)
    async with app.run_test(size=size) as pilot:
        assert "Invalid definition" in str(app.query_one("#board-slots", DataTable).get_row("0"))
        board = await open_slot(app, pilot, 0)
        board.query_one("#socs", TextArea).load_text("- name: repaired")
        board.query_one("#clock_source", Select).value = "fpga"
        await pilot.click("#save-board")
        await pilot.pause()
        await pilot.press("ctrl+s")
    assert app.return_value.boards[0].socs[0].name == "repaired"
