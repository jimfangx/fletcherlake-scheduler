"""Cancellation cannot let the service close its pool while a DB thread is still running."""

import asyncio
import threading

import pytest
from fl_scheduler.db.async_calls import database_call


async def test_database_call_reaps_transaction_before_cancellation_returns():
    entered, release, finished = threading.Event(), threading.Event(), threading.Event()

    def transaction():
        entered.set()
        assert release.wait(timeout=5)
        finished.set()

    task = asyncio.create_task(database_call(transaction))
    try:
        assert await asyncio.to_thread(entered.wait, 2)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done() and not finished.is_set()
    finally:
        release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert finished.is_set()
