"""Elapsed times, queue order and retention display follow the daemon snapshot."""

from datetime import timedelta

from fl_cli.dashboard.tables import tables

from tests.dashboard_snapshot import snapshot


def test_snapshot_tables_display_inventory_and_authoritative_timing(config):
    data = snapshot(config)
    views = tables(data)
    assert views["boards"].rows[0].cells[1:4] == ("fletcherlake", "xcvu9p", "RUNNING")
    assert views["jobs"].rows[0].cells[0] == "[bold]jim"
    assert views["jobs"].rows[0].cells[3] == "01:00:00"
    assert views["jobs"].rows[1].cells[3] == "—"
    assert [row.cells[4] for row in views["artifacts"].rows] == [
        "After completion",
        "29d 11:00:00",
        "Due",
        "Deleted",
    ]
    assert all(row.cells[3] == "1.0 GiB" for row in views["artifacts"].rows)
    # Completion freezes elapsed time even if later snapshots arrive.
    data.jobs[0].finished_at = data.timestamp
    data.timestamp += timedelta(days=1)
    assert tables(data)["jobs"].rows[0].cells[3] == "01:00:00"


def test_queue_positions_are_not_resorted_and_missing_metadata_remains_visible(config):
    data = snapshot(config)
    first, second = [job.spec.job_id for job in data.jobs]
    data.queues["board-0"] = [first, second]
    rows = tables(data)["queues"].rows
    assert [row.cells[2] for row in rows] == [str(first), str(second)]
    assert [row.cells[1] for row in rows] == ["1", "2"]
    data.jobs = []
    assert tables(data)["queues"].rows[0].cells[3] == "—"
    assert len(tables(data)["artifacts"].rows) == 4
