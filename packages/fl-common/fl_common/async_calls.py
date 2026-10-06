"""Reap synchronous work before propagating cancellation to its async caller."""

import asyncio
from collections.abc import Callable


async def background_call[**Parameters, Result](
    operation: Callable[Parameters, Result], *args: Parameters.args, **kwargs: Parameters.kwargs
) -> Result:
    task = asyncio.create_task(asyncio.to_thread(operation, *args, **kwargs))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        await asyncio.gather(task, return_exceptions=True)
        raise
