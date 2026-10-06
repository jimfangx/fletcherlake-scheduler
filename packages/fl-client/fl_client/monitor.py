"""Resume byte offsets and event cursors across temporary scheduler outages."""

import time
from collections.abc import Callable, Iterator
from typing import Any
from uuid import UUID

import httpx
from fl_common.errors import PlatformError
from fl_common.models import JobState
from fl_common.models.logs import MAX_LOG_BYTES, MAX_OFFSET, LogPage, LogStream

from .api import RemoteClient

STATE_NAMES = frozenset(state.value for state in JobState)


class Monitor:
    def __init__(
        self,
        client: RemoteClient,
        *,
        sleep: Callable[[float], None] = time.sleep,
        interval: float = 0.5,
    ) -> None:
        self.client, self.sleep, self.interval = client, sleep, interval

    def request(self, path: str, **kwargs: Any) -> Any:
        while True:
            try:
                return self.client.request("GET", path, **kwargs)
            except httpx.TransportError:
                self.sleep(self.interval)
            except PlatformError as error:
                if error.details.get("status") not in {408, 429, 502, 503, 504}:
                    raise
                self.sleep(self.interval)

    def logs(
        self,
        job_id: UUID,
        *,
        stream: LogStream = "stdout",
        offset: int = 0,
        follow: bool = False,
    ) -> Iterator[bytes]:
        if not 0 <= offset <= MAX_OFFSET or stream not in {"stdout", "stderr"}:
            raise ValueError("Invalid log stream or byte offset")
        stop: int | None = None
        file_id: str | None = None
        while True:
            limit = MAX_LOG_BYTES if stop is None else max(1, min(MAX_LOG_BYTES, stop - offset))
            page = LogPage.model_validate(
                self.request(
                    f"/api/jobs/{job_id}/logs",
                    params={"stream": stream, "offset": offset, "limit": limit},
                )
            )
            if (page.job_id, page.stream, page.offset) != (job_id, stream, offset):
                raise PlatformError("LOG_INTEGRITY", "Scheduler returned a different log range")
            if page.head:
                if not page.head.retained:
                    raise PlatformError("ARTIFACT_EXPIRED", "Log retention ended")
                if file_id is not None and file_id != page.head.file_id:
                    raise PlatformError("LOG_CHANGED", "Log file identity changed while reading")
                file_id = page.head.file_id
                if not follow and stop is None:
                    stop = page.head.size_bytes
            if page.chunk:
                data = page.chunk.data()
                if len(data) > limit:
                    raise PlatformError(
                        "LOG_INTEGRITY", "Scheduler exceeded the requested byte limit"
                    )
                if data:
                    yield data
                offset = page.chunk.next_offset
                if page.chunk.eof or (stop is not None and offset >= stop):
                    return
                if data:
                    continue
            self.sleep(self.interval)

    def states(self, job_id: UUID) -> Iterator[str]:
        """Replay durable transitions, but stop only on authoritative job metadata."""
        cursor = 0
        previous: str | None = None
        while True:
            events = self.request(
                f"/api/jobs/{job_id}/events", params={"after": cursor, "limit": 100}
            )
            for event in events:
                event_id = int(event["event_id"])
                if event_id <= cursor:
                    raise PlatformError("EVENT_ORDER", "Scheduler event cursor did not advance")
                state = str(event["type"]).removeprefix("JOB_")
                if state in STATE_NAMES and state != previous:
                    yield state
                    previous = state
                cursor = event_id
            if len(events) == 100:
                continue
            status = self.request(f"/api/jobs/{job_id}")
            state = JobState(status["state"])
            if state != previous:
                yield state.value
                previous = state.value
            if state.terminal:
                return
            self.sleep(self.interval)
