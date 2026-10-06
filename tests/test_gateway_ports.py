"""Port reservations respect live listeners and recover after hard process death."""

import multiprocessing
import socket
import time
from pathlib import Path

import pytest
from fl_gateway.ports import reserve


def hold_port(root, port, ready):
    with reserve(root, port, port, deadline=time.monotonic() + 10):
        ready.send(True)
        ready.recv()


def test_occupied_port_wait_expires_and_releases_lock(tmp_path):
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        port = listener.getsockname()[1]
        with (
            pytest.raises(TimeoutError, match="waiting for a BBCP port"),
            reserve(tmp_path, port, port, deadline=time.monotonic() + 0.05),
        ):
            pytest.fail("An occupied port was reserved")
    with reserve(tmp_path, port, port, deadline=time.monotonic() + 1) as acquired:
        assert acquired == port


def test_killed_owner_releases_port_reservation(tmp_path: Path):
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    context = multiprocessing.get_context("spawn")
    parent, child = context.Pipe()
    worker = context.Process(target=hold_port, args=(tmp_path, port, child))
    worker.start()
    child.close()
    try:
        assert parent.poll(10), "Port owner did not become ready"
        assert parent.recv() is True
        with (
            pytest.raises(TimeoutError),
            reserve(tmp_path, port, port, deadline=time.monotonic() + 0.05),
        ):
            pytest.fail("Another process reserved the owned port")
        worker.kill()
        worker.join(5)
        assert not worker.is_alive()
        with reserve(tmp_path, port, port, deadline=time.monotonic() + 1) as acquired:
            assert acquired == port
    finally:
        if worker.is_alive():
            worker.kill()
        worker.join(5)
        worker.close()
        parent.close()
