"""An in-memory configuration editor. Only validated Save returns data to provisioning."""

from copy import deepcopy
from typing import Any

from fl_common.errors import PlatformError
from fl_common.models import BoardConfig, ClusterConfig
from pydantic import ValidationError
from rich.text import Text
from textual.app import App, ComposeResult
from textual.containers import Horizontal, VerticalScroll
from textual.widgets import (
    Button,
    DataTable,
    Footer,
    Header,
    Input,
    Label,
    Static,
    TabbedContent,
    TabPane,
    TextArea,
)

from .board import BoardEditor
from .fields import device_names, read_yaml, validation_message, yaml_field


class InventoryEditor(App[ClusterConfig | None]):
    TITLE = "Fletcherlake configuration"
    CSS_PATH = "editor.tcss"
    BINDINGS = [("escape", "cancel", "Cancel"), ("ctrl+s", "save", "Save configuration")]

    def __init__(self, initial: dict[str, Any]) -> None:
        super().__init__()
        self.initial = deepcopy(initial)
        boards = self.initial.get("boards")
        if not isinstance(boards, list) or not 1 <= len(boards) <= 3:
            raise PlatformError("INVALID_INVENTORY", "Provide one to three explicit board slots")
        if any(board is not None and not isinstance(board, dict) for board in boards):
            raise PlatformError("INVALID_INVENTORY", "Board slots must be mappings or null")
        self.boards: list[dict[str, Any] | None] = deepcopy(boards) + [None] * (3 - len(boards))

    def compose(self) -> ComposeResult:
        yield Header()
        with TabbedContent():
            with TabPane("Host", id="host-tab"):
                with VerticalScroll():
                    for key, label in (
                        ("apple_model", "Apple model"),
                        ("mac_address", "MAC address"),
                    ):
                        yield Label(label)
                        yield Input(str(self.initial.get(key) or ""), id=key)
                    yield Label("Operating system (YAML)")
                    yield yaml_field("os", self.initial.get("os", {}))
            with TabPane("Boards", id="boards-tab"):
                yield Static(
                    "Select a slot and press Enter to edit. Empty slots stay disconnected."
                )
                yield DataTable(id="board-slots", cursor_type="row", zebra_stripes=True)
                with Horizontal(classes="form-actions"):
                    yield Button("Add / edit selected", id="edit-slot")
                    yield Button("Clear selected", id="clear-slot")
            with TabPane("Tools & power", id="tools-tab"):
                with VerticalScroll():
                    yield Label("Tool paths, versions and Vivado edition (YAML)")
                    yield yaml_field("environment", self.initial.get("environment", {}))
                    yield Label("Optional power control scaffold (YAML; null disables it)")
                    yield yaml_field("power_control", self.initial.get("power_control"))
        yield Static("", id="inventory-error", markup=False)
        with Horizontal(classes="form-actions"):
            yield Button("Save configuration", id="save-inventory", variant="primary")
            yield Button("Cancel", id="cancel-inventory")
        yield Footer()

    def on_mount(self) -> None:
        table = self.query_one("#board-slots", DataTable)
        table.add_columns("Slot", "Board", "Backend", "SoCs", "FPGAs")
        self.render_boards()

    def render_boards(self) -> None:
        table = self.query_one("#board-slots", DataTable)
        selected = table.cursor_row
        table.clear()
        for index, board in enumerate(self.boards):
            cells = (
                str(index + 1),
                str(board.get("board_id", "")) if board else "Not connected",
                str(board.get("backend", "")) if board else "—",
                device_names(board, "socs", "name"),
                device_names(board, "fpgas", "model"),
            )
            table.add_row(*(Text(cell) for cell in cells), key=str(index))
        table.move_cursor(row=selected)

    def edit_slot(self) -> None:
        slot = self.query_one("#board-slots", DataTable).cursor_row

        def accepted(board: BoardConfig | None) -> None:
            if board is not None:
                self.boards[slot] = board.model_dump(mode="json")
                self.render_boards()

        self.push_screen(BoardEditor(slot, self.boards[slot]), accepted)

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        self.edit_slot()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "edit-slot":
            self.edit_slot()
        elif event.button.id == "clear-slot":
            slot = self.query_one("#board-slots", DataTable).cursor_row
            self.boards[slot] = None
            self.render_boards()
        elif event.button.id == "save-inventory":
            self.action_save()
        elif event.button.id == "cancel-inventory":
            self.action_cancel()

    def action_save(self) -> None:
        data = deepcopy(self.initial)
        data["boards"] = self.boards
        try:
            for key in ("apple_model", "mac_address"):
                data[key] = self.query_one(f"#{key}", Input).value.strip()
                if not data[key]:
                    raise ValueError(f"{key}: Enter a value")
            for key in ("os", "environment", "power_control"):
                data[key] = read_yaml(self.query_one(f"#{key}", TextArea))
            config = ClusterConfig.model_validate(data)
        except (ValidationError, ValueError) as error:
            self.query_one("#inventory-error", Static).update(Text(validation_message(error)))
            return
        self.exit(config)

    def action_cancel(self) -> None:
        self.exit(None)
