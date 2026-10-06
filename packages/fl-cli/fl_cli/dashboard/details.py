"""Selected job information comes from the same snapshot as its table row."""

from fl_common.models import JobRecord
from rich.text import Text
from textual.app import ComposeResult
from textual.containers import VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, Static

from .formatting import timestamp


class JobDetails(ModalScreen[None]):
    BINDINGS = [("escape", "close", "Close")]

    def __init__(self, job: JobRecord) -> None:
        super().__init__()
        self.job = job

    def compose(self) -> ComposeResult:
        spec, job = self.job.spec, self.job
        fields = (
            ("Job", str(spec.job_id)),
            ("Owner", spec.owner),
            ("Board", job.board_id),
            ("State", str(job.state)),
            ("Submitted", timestamp(spec.submitted_at)),
            ("Started", timestamp(job.started_at)),
            ("Finished", timestamp(job.finished_at)),
            ("Priority", str(spec.priority)),
            ("Timeout", f"{spec.run_timeout_seconds} seconds"),
            ("Retention", f"{spec.collateral_ttl_days} days after completion"),
            ("Error", job.error or "—"),
        )
        with VerticalScroll(id="job-details"):
            yield Static(Text("\n\n".join(f"{label}: {value}" for label, value in fields)))
            yield Button("Close", id="close-details")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.action_close()

    def action_close(self) -> None:
        self.dismiss()
