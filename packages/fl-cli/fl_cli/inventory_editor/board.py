"""Explicit board form; a new board has no implicit identifier or backend."""

from copy import deepcopy
from typing import Any

from fl_common.models import BoardConfig
from pydantic import ValidationError
from rich.text import Text
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Label, Select, Static, TabbedContent, TabPane, TextArea

from .fields import read_yaml, validation_message, yaml_field

NUMBERS = (
    ("num_vrails", "Voltage rails", 0),
    ("num_vsense", "Voltage sensors", 0),
    ("num_isense", "Current sensors", 0),
    ("firmware_timeout_seconds", "Firmware command timeout (seconds)", 60),
)
YAML_FIELDS: tuple[tuple[str, str, object], ...] = (
    ("fpgas", "FPGAs: list of model/device mappings", []),
    ("socs", "SoCs: list of name/device mappings", []),
    ("device_mapping", "Device mapping: names to device paths", {}),
    ("firmware_commands", "Firmware operations: names to argv lists", {}),
)


class BoardEditor(ModalScreen[BoardConfig | None]):
    BINDINGS = [("escape", "cancel", "Cancel"), ("ctrl+s", "save_board", "Save board")]

    def __init__(self, slot: int, board: dict[str, Any] | None) -> None:
        super().__init__()
        self.slot, self.initial = slot, deepcopy(board or {})

    def compose(self) -> ComposeResult:
        with Vertical(id="board-form"):
            yield Static(f"Board slot {self.slot + 1}", classes="form-heading")
            with TabbedContent():
                with TabPane("Identity & sensors"):
                    with VerticalScroll():
                        yield from self.identity_fields()
                with TabPane("Devices & firmware"):
                    with VerticalScroll():
                        yield Static(
                            "Use standard YAML. Firmware commands are argv lists, without a shell."
                        )
                        for key, label, default_yaml in YAML_FIELDS:
                            yield Label(label)
                            yield yaml_field(key, self.initial.get(key, default_yaml))
            yield Static("", id="board-error", markup=False)
            with Horizontal(classes="form-actions"):
                yield Button("Save board", id="save-board", variant="primary")
                yield Button("Cancel", id="cancel-board")

    def identity_fields(self) -> ComposeResult:
        yield Label("Board ID")
        yield Input(str(self.initial.get("board_id", "")), id="board_id")
        yield Label("Backend (choose explicitly)")
        backend = self.initial.get("backend")
        yield Select.from_values(
            ["lilikoi", "mock"],
            value=backend if backend in {"lilikoi", "mock"} else Select.BLANK,
            id="backend",
        )
        yield Label("Clock source")
        clocks = ["on_chip", "fpga", "external_clock_gen"]
        clock = self.initial.get("clock_source", "on_chip")
        yield Select.from_values(
            clocks, value=clock if clock in clocks else Select.BLANK, id="clock_source"
        )
        for key, label, default in NUMBERS:
            yield Label(label)
            yield Input(str(self.initial.get(key, default)), id=key, type="integer")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "cancel-board":
            self.action_cancel()
        elif event.button.id == "save-board":
            self.save_board()

    def save_board(self) -> None:
        data = deepcopy(self.initial)
        data["board_id"] = self.query_one("#board_id", Input).value.strip()
        for key in ("backend", "clock_source"):
            selected = self.query_one(f"#{key}", Select).value
            data[key] = None if selected is Select.BLANK else selected
        try:
            for key, label, _ in NUMBERS:
                try:
                    data[key] = int(self.query_one(f"#{key}", Input).value)
                except ValueError as error:
                    raise ValueError(f"{label}: Enter a whole number") from error
            for key, _, empty in YAML_FIELDS:
                data[key] = read_yaml(self.query_one(f"#{key}", TextArea), empty=empty)
            board = BoardConfig.model_validate(data)
        except (ValidationError, ValueError) as error:
            self.query_one("#board-error", Static).update(Text(validation_message(error)))
            return
        self.dismiss(board)

    def action_cancel(self) -> None:
        self.dismiss(None)

    def action_save_board(self) -> None:
        self.save_board()
