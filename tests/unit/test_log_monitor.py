"""Byte offsets, integrity and authoritative state survive client retries."""

from uuid import uuid4

import httpx
import pytest
from fl_client.monitor import Monitor
from fl_common.errors import PlatformError
from fl_common.models.logs import LogChunk, LogHead, LogPage


@pytest.mark.parametrize("follow", [False, True])
def test_binary_log_pages_retry_without_duplicates_and_follow_new_bytes(follow):
    job_id = uuid4()
    original = bytes(range(256)) * 515 + "🧪".encode()
    tail = b"\xff\x00new bytes\n"

    class Client:
        terminal = False
        lost = False
        waiting = True
        calls = []

        def request(self, method, path, *, params):
            assert method == "GET" and path == f"/api/jobs/{job_id}/logs"
            offset, limit = params["offset"], params["limit"]
            self.calls.append(offset)
            if offset and not self.lost:
                self.lost = True
                raise httpx.ReadError("Injected lost response")
            data = original + (tail if self.terminal else b"")
            head = LogHead(
                job_id=job_id,
                stream="stdout",
                file_id="42",
                size_bytes=len(data),
                terminal=self.terminal,
                retained=True,
            )
            if self.waiting:
                self.waiting = False
                return LogPage(
                    job_id=job_id, stream="stdout", offset=offset, state="WAITING", head=head
                ).model_dump(mode="json")
            return LogPage(
                job_id=job_id,
                stream="stdout",
                offset=offset,
                state="READY",
                head=head,
                chunk=LogChunk.build(head, offset, data[offset : offset + limit]),
            ).model_dump(mode="json")

    client = Client()
    sleeps = []

    def sleep(seconds):
        sleeps.append(seconds)
        if len(sleeps) >= 2:
            client.terminal = True

    chunks = list(Monitor(client, sleep=sleep).logs(job_id, follow=follow))
    assert b"".join(chunks) == original + (tail if follow else b"")
    assert client.calls[:4] == [0, 0, 65536, 65536]


@pytest.mark.parametrize("tamper", ["sha", "scope", "identity"])
def test_log_validation_rejects_corrupt_or_changed_source(tamper):
    job_id = uuid4()
    head = LogHead(
        job_id=job_id, stream="stdout", file_id="1", size_bytes=2, terminal=True, retained=True
    )

    class Client:
        def request(self, method, path, *, params):
            offset = params["offset"]
            source = (
                head.model_copy(update={"file_id": "2"})
                if tamper == "identity" and offset
                else head
            )
            page = LogPage(
                job_id=job_id,
                stream="stdout",
                offset=offset,
                state="READY",
                head=source,
                chunk=LogChunk.build(source, offset, b"x"),
            ).model_dump(mode="json")
            if tamper == "sha":
                page["chunk"]["data_b64"] = "eQ=="
            if tamper == "scope":
                page["job_id"] = str(uuid4())
            return page

    iterator = Monitor(Client()).logs(job_id, follow=True)
    if tamper == "identity":
        assert next(iterator) == b"x"
        with pytest.raises(PlatformError, match="identity changed"):
            next(iterator)
    else:
        with pytest.raises(ValueError):
            next(iterator)


def test_follow_replays_events_but_waits_for_authoritative_terminal_metadata():
    job_id = uuid4()

    class Client:
        status_calls = 0
        failed = False

        def request(self, method, path, **kwargs):
            if path.endswith("/events"):
                if not self.failed:
                    self.failed = True
                    raise PlatformError("AUTH_UNAVAILABLE", "Provider outage", status=503)
                after = kwargs["params"]["after"]
                return [
                    {"event_id": index, "type": "JOB_" + state}
                    for index, state in enumerate(["QUEUED", "RUNNING", "SUCCEEDED"], 1)
                    if index > after
                ]
            self.status_calls += 1
            return {"state": "SUCCEEDED" if self.status_calls > 1 else "RUNNING"}

    client = Client()
    states = list(Monitor(client, sleep=lambda _: None).states(job_id))
    assert states == ["QUEUED", "RUNNING", "SUCCEEDED", "RUNNING", "SUCCEEDED"]
    assert client.status_calls == 2


def test_follow_stops_immediately_when_membership_is_revoked():
    class Client:
        def request(self, *args, **kwargs):
            raise PlatformError("FORBIDDEN", "Membership revoked", status=403)

    with pytest.raises(PlatformError, match="revoked"):
        next(
            Monitor(Client(), sleep=lambda _: pytest.fail("Revocation cannot be retried")).states(
                uuid4()
            )
        )
