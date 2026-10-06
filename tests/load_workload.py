"""Build a mixed-priority, mixed-cost workload and verify authoritative per-board history."""

from collections import Counter
from concurrent.futures import ThreadPoolExecutor

from fl_common.models import JobConfig, ResourceConstraints
from fl_scheduler.db.models import Assignment, Job
from fl_scheduler.scheduler.placement import Placement
from sqlalchemy import select


def submit_many(db, owner, boards, count=315):
    def submit(index):
        explicit = boards[(index // 9) % len(boards)] if index % 9 == 0 else None
        return Placement(db).submit(
            JobConfig(
                priority=(index * 7) % 10 - 4,
                run_timeout=35 * 86400 if index % 13 == 0 else 30,
                resource_constraints=ResourceConstraints(
                    board=explicit, soc="fletcherlake", fpga="xcvu9p"
                ),
            ),
            owner,
        )

    with ThreadPoolExecutor(max_workers=4) as pool:
        return list(pool.map(submit, range(count)))


def states(db):
    with db.transaction() as session:
        return dict(session.execute(select(Job.job_id, Job.state)).all())


def all_states(db, ids, allowed):
    current = states(db)
    return all(current.get(job_id) in allowed for job_id in ids)


def assignments(db, specs):
    ids = {spec.job_id for spec in specs}
    with db.transaction() as session:
        assigned = {
            row.job_id: row.board_id
            for row in session.scalars(select(Assignment))
            if row.job_id in ids
        }
    assert len(assigned) == len(specs)
    counts = Counter(assigned.values())
    assert len(counts) == 9 and max(counts.values()) - min(counts.values()) <= 3
    for spec in specs:
        if spec.resource_constraints.board:
            assert assigned[spec.job_id] == spec.resource_constraints.board
    return assigned


def verify_execution(services, specs, assigned, canceled):
    expected = {spec.job_id: spec for spec in specs}
    observed = set()
    for service in services:
        for board_id in service.workers:
            jobs = [
                spec
                for spec in specs
                if assigned[spec.job_id] == board_id and spec.job_id not in canceled
            ]
            ordered = sorted(jobs, key=lambda spec: (spec.priority, spec.submitted_at, spec.job_id))
            actual = [
                event.job_id
                for event in service.db.events(limit=100000)
                if event.type == "JOB_RUNNING"
                and event.board_id == board_id
                and event.job_id in expected
            ]
            assert actual == [spec.job_id for spec in ordered]
        for record in service.db.jobs():
            job_id = record.spec.job_id
            if job_id not in expected:
                continue
            assert job_id not in observed and record.board_id == assigned[job_id]
            observed.add(job_id)
            events = service.db.job_events(job_id)
            starts = sum(event.type == "JOB_RUNNING" for event in events)
            if job_id in canceled:
                assert record.state == "CANCELED" and starts == 0
            else:
                assert record.state == "SUCCEEDED" and starts == 1
                assert sum(event.type == "JOB_SUCCEEDED" for event in events) == 1
    assert observed == set(expected)
