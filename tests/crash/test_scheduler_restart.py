"""A fresh scheduler process reconciles hardware and durable work after SIGKILL."""

import asyncio
import json
from uuid import UUID

import httpx
import pytest
from fl_agent.configuration import confirm_config, write_config
from fl_agent.connection import SchedulerConnection
from fl_agent.hardware.base import RunResult
from fl_agent.hardware.mock import MockBoardBackend
from fl_agent.service import AgentService
from fl_common.errors import PlatformError
from fl_common.files import atomic_write
from fl_common.models.scheduler import ClusterSnapshot
from fl_scheduler.agents.commands import Commands
from fl_scheduler.agents.reconcile import Reconciler
from fl_scheduler.api.control import Control
from fl_scheduler.db.models import Command, Event
from fl_scheduler.registry import Registry
from sqlalchemy import select

from tests.connected import state, until
from tests.scheduler_runtime import SchedulerProcess, projection


async def test_sigkill_scheduler_preserves_execution_and_replays_durable_work(
    scheduler_db, auth_stack, config, tmp_path, monkeypatch
):
    sessions, _, _, provider, _ = auth_stack
    tokens = await sessions.issue(provider.identity)
    principal = await sessions.authenticate(tokens.access_token.get_secret_value())
    agent_token = "scheduler restart fixture agent credential"
    registered = Registry(scheduler_db).register(config, agent_token)
    root = tmp_path / "agent"
    write_config(root, registered)
    confirm_config(root)
    boards = {item.board_id: MockBoardBackend() for item in registered.boards if item}
    first, second, _ = boards.values()

    def hold_completion(board):
        released = asyncio.Event()

        async def complete():
            await released.wait()
            return RunResult(passed=True)

        monkeypatch.setattr(board, "wait_for_completion", complete)
        return released

    finish, finish_offline = hold_completion(first), hold_completion(second)
    scheduler = SchedulerProcess(scheduler_db, tmp_path / "scheduler")
    monkeypatch.setenv("SSL_CERT_FILE", str(scheduler.root / "server.pem"))
    service = AgentService(registered, root, root / "run", boards)
    service.scheduler_link = SchedulerConnection(service, scheduler.origin, agent_token)
    commands = Commands(scheduler_db)
    started = False
    try:
        scheduler.start()
        await scheduler.ready()
        await service.start()
        started = True
        await until(lambda: service.scheduler_link.connected)
        async with httpx.AsyncClient(
            base_url=scheduler.origin,
            verify=scheduler.context,
            trust_env=False,
            headers={"Authorization": "Bearer " + tokens.access_token.get_secret_value()},
        ) as client:

            async def submit(board_id, priority=0):
                response = await client.post(
                    "/api/jobs",
                    json={
                        "config": {
                            "resource_constraints": {"board": board_id},
                            "priority": priority,
                            "run_timeout": 180,
                        }
                    },
                )
                assert response.status_code == 201
                return UUID(response.json()["job_id"])

            board_ids = list(boards)
            running, offline = await submit(board_ids[0]), await submit(board_ids[1])
            await until(
                lambda: all(state(scheduler_db, item) == "RUNNING" for item in (running, offline)),
                seconds=30,
            )
            queued, canceled = await submit(board_ids[0], 1), await submit(board_ids[0], 2)
            await until(
                lambda: all(state(scheduler_db, item) == "QUEUED" for item in (queued, canceled)),
                seconds=30,
            )
            await until(lambda: not commands.pending(registered.cluster_id))
            await until(
                lambda: (
                    projection(scheduler_db, registered.cluster_id)[0] == service.db.last_sequence()
                )
            )
            cursor, previous_session = projection(scheduler_db, registered.cluster_id)
            previous_pid, stops = scheduler.process.pid, first.calls.count("stop")
            await asyncio.to_thread(scheduler.stop, kill=True)
            assert scheduler.process.returncode == -9
            await until(lambda: not service.scheduler_link.connected)
            finish_offline.set()
            await until(lambda: service.db.get(offline).state == "SUCCEEDED")
            assert first.running and service.db.get(running).state == "RUNNING"
            assert first.calls.count("stop") == stops
            assert state(scheduler_db, offline) == "RUNNING"
            assert projection(scheduler_db, registered.cluster_id)[0] == cursor

            # These committed rows model lost ACKs and accepted work awaiting a restart.
            with scheduler_db.transaction() as transaction:
                replayed = list(transaction.scalars(select(Command)))
                assert len(replayed) == 4
                command_ids = [str(item.message_id) for item in replayed]
                for item in replayed:
                    item.acknowledged_at = None
                    item.response = None
            Control(scheduler_db).cancel(canceled, principal)
            scheduler.start()
            assert scheduler.process.pid != previous_pid
            await scheduler.ready()
            assert (await client.get("/api/auth/me")).status_code == 200
            await until(lambda: service.scheduler_link.connected, seconds=40)
            await until(lambda: state(scheduler_db, offline) == "SUCCEEDED")
            await until(lambda: state(scheduler_db, canceled) == "CANCELED")
            assert first.running and first.calls.count("stop") == stops
            fresh = await submit(board_ids[2])
            await until(lambda: state(scheduler_db, fresh) == "SUCCEEDED", seconds=30)
            finish.set()
            await until(
                lambda: all(state(scheduler_db, item) == "SUCCEEDED" for item in (running, queued))
            )
            await until(
                lambda: (
                    projection(scheduler_db, registered.cluster_id)[0] == service.db.last_sequence()
                )
            )
            await until(lambda: not commands.pending(registered.cluster_id))
            current_cursor, current_session = projection(scheduler_db, registered.cluster_id)
            assert current_session != previous_session
            with pytest.raises(PlatformError) as error:
                Reconciler(scheduler_db).snapshot(
                    registered.cluster_id,
                    previous_session,
                    ClusterSnapshot.model_validate(service.snapshot()),
                )
            assert error.value.code == "STALE_SESSION"
            for item in (running, offline, queued, fresh):
                events = [event.type for event in service.db.job_events(item)]
                assert events.count("JOB_RUNNING") == events.count("JOB_SUCCEEDED") == 1
                assert "JOB_INTERRUPTED" not in events
                with scheduler_db.transaction() as transaction:
                    assert (
                        len(
                            transaction.scalars(
                                select(Event).where(
                                    Event.job_id == item, Event.type == "JOB_SUCCEEDED"
                                )
                            ).all()
                        )
                        == 1
                    )
            assert not service.db.job_events(canceled, "JOB_RUNNING")
            atomic_write(
                tmp_path / "scheduler-restart-report.json",
                json.dumps(
                    {
                        "killed_pid": previous_pid,
                        "restarted_pid": scheduler.process.pid,
                        "cursor_before": cursor,
                        "cursor_after": current_cursor,
                        "replayed_enqueue_commands": command_ids,
                        "successful_jobs": [
                            str(item) for item in (running, offline, queued, fresh)
                        ],
                        "canceled_job": str(canceled),
                    },
                    indent=2,
                ).encode(),
            )
    finally:
        if started:
            await service.stop()
        await asyncio.to_thread(scheduler.stop)
