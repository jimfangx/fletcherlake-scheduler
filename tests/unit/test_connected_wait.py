import asyncio
import threading

import pytest

from tests.connected import until


async def test_database_poll_allows_the_event_loop_to_continue():
    entered, released = threading.Event(), threading.Event()

    def predicate():
        entered.set()
        assert released.wait(1), "The event loop could not release the database poll"
        return True

    async def release():
        assert await asyncio.to_thread(entered.wait, 1)
        released.set()

    await asyncio.gather(until(predicate, poll_in_thread=True), release())


async def test_wait_failure_identifies_the_stage():
    with pytest.raises(TimeoutError, match="queued jobs canceled"):
        await until(lambda: False, seconds=0.01, description="queued jobs canceled")
