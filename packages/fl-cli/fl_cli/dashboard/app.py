"""Local status application: one in-flight request, five views, explicit stale-data banner."""

import asyncio

import httpx
from fl import Cluster
from fl_common.models.scheduler import ClusterSnapshot
from rich.text import Text
from textual.app import App, ComposeResult
from textual.timer import Timer
from textual.widgets import DataTable, Footer, Header, Static, TabbedContent, TabPane

from .details import JobDetails
from .formatting import timestamp
from .metrics import Metrics
from .tables import Table, tables


def update_table(widget: DataTable[Text], view: Table) -> None:
    selected = (
        widget.ordered_rows[widget.cursor_row].key.value
        if widget.row_count and widget.cursor_row < widget.row_count
        else None
    )
    widget.clear()
    if not widget.columns:
        widget.add_columns(*view.columns)
    for index, row in enumerate(view.rows):
        widget.add_row(*(Text(cell) for cell in row.cells), key=row.key)
        if selected == row.key:
            widget.move_cursor(row=index)


class Dashboard(App[None]):
    TITLE = "Fletcherlake agent"
    CSS_PATH = "dashboard.tcss"
    BINDINGS = [("q", "quit", "Quit"), ("r", "refresh_snapshot", "Refresh")]

    def __init__(self, cluster: Cluster) -> None:
        super().__init__()
        self.cluster = cluster
        self.snapshot: ClusterSnapshot | None = None
        self.refresh_timer: Timer | None = None
        self._refreshing = False

    def compose(self) -> ComposeResult:
        yield Header()
        yield Static("Connecting to local agent…", id="connection", markup=False)
        with TabbedContent():
            for name in ("Boards", "Jobs", "Queues", "Artifacts"):
                with TabPane(name, id=f"{name.lower()}-tab"):
                    yield DataTable(id=name.lower(), cursor_type="row", zebra_stripes=True)
            with TabPane("System", id="system-tab"):
                yield Metrics(id="system")
        yield Footer()

    def on_mount(self) -> None:
        self.refresh_timer = self.set_interval(2, self.action_refresh_snapshot)
        self.action_refresh_snapshot()

    def action_refresh_snapshot(self) -> None:
        # Canceling an async worker cannot stop its in-flight synchronous socket request.
        # Skip refresh ticks instead of accumulating threads or overlapping requests.
        if not self._refreshing:
            self._refreshing = True
            self.run_worker(self._refresh())

    async def _refresh(self) -> None:
        try:
            data = await asyncio.to_thread(self.cluster.status)
            snapshot = ClusterSnapshot.model_validate(data)
            for name, view in tables(snapshot).items():
                update_table(self.query_one(f"#{name}", DataTable), view)
            self.query_one("#system", Metrics).update_snapshot(snapshot)
            self.snapshot = snapshot
            self.query_one("#connection", Static).update(
                Text(
                    f"{snapshot.cluster.apple_model} | {snapshot.state} | "
                    f"Snapshot: {timestamp(snapshot.timestamp)}"
                )
            )
        except (httpx.HTTPError, OSError, ValueError):
            last = timestamp(self.snapshot.timestamp) if self.snapshot else "never received"
            self.query_one("#connection", Static).update(
                Text(f"Agent unavailable | Last snapshot: {last} | Retrying; press r to refresh")
            )
        finally:
            self._refreshing = False

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        if event.data_table.id != "jobs" or self.snapshot is None:
            return
        job = next(
            (job for job in self.snapshot.jobs if str(job.spec.job_id) == event.row_key.value),
            None,
        )
        if job:
            self.push_screen(JobDetails(job))
