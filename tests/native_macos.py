"""Native launchd effects injected in lifecycle tests; no Linux service substitution."""

import plistlib
from collections.abc import Callable
from pathlib import Path

from fl.macos.provisioning import LABEL


class Launchd:
    def __init__(self, calls: list, *, agent_registered: bool = True) -> None:
        self.calls = calls
        self.registered = {LABEL} if agent_registered else set()
        self.on_stop: Callable[[], None] | None = None
        self.on_start: Callable[[], None] | None = None

    def loaded(self, label: str) -> bool:
        return label in self.registered

    def run(self, argv: list[str]) -> None:
        self.calls.append(argv)
        if argv[1] == "bootstrap":
            label = plistlib.loads(Path(argv[3]).read_bytes())["Label"]
            self.registered.add(label)
            if label == LABEL and self.on_start:
                self.on_start()
        elif argv[1] == "bootout":
            label = argv[2].removeprefix("system/")
            self.registered.remove(label)
            if label == LABEL and self.on_stop:
                self.on_stop()
