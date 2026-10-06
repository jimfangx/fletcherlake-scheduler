"""System gauges show the daemon's measurements, including its scheduler link status."""

from fl_common.models.scheduler import ClusterSnapshot
from rich.text import Text
from textual.app import ComposeResult
from textual.containers import VerticalScroll
from textual.widgets import ProgressBar, Static

from .formatting import size


class Metrics(VerticalScroll):
    def compose(self) -> ComposeResult:
        for name in ("cpu", "ram", "disk"):
            yield Static("Waiting for metrics…", id=f"{name}-label")
            yield ProgressBar(total=100, show_eta=False, id=f"{name}-bar")
        yield Static("", id="scheduler-link")

    def update_snapshot(self, snapshot: ClusterSnapshot) -> None:
        metrics = snapshot.system
        used = max(0, metrics.disk_total - metrics.disk_free)
        disk_percent = min(100, used / metrics.disk_total * 100) if metrics.disk_total else 0
        values = (
            ("cpu", metrics.cpu * 100, f"CPU: {metrics.cpu:.0%}"),
            ("ram", metrics.memory * 100, f"RAM: {metrics.memory:.0%}"),
            ("disk", disk_percent, f"Disk used: {size(used)} / {size(metrics.disk_total)}"),
        )
        for name, percent, label in values:
            self.query_one(f"#{name}-label", Static).update(Text(label))
            self.query_one(f"#{name}-bar", ProgressBar).update(progress=percent)
        connected = "Connected" if metrics.scheduler_connected else "Disconnected"
        self.query_one("#scheduler-link", Static).update(Text(f"Scheduler: {connected}"))
