"""Real TLS nginx listeners on distinct loopback aliases, with an observable upstream."""

import socket
import ssl
import subprocess
import threading
import time
from contextlib import contextmanager

import uvicorn
from fastapi import FastAPI, Request, WebSocket
from fl_deploy.native import substitute

from tests.deployment import nginx_config, run


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@contextmanager
def upstream():
    received = []
    app = FastAPI()

    @app.websocket("/api/agents/ws")
    async def echo(websocket: WebSocket):
        await websocket.accept()
        await websocket.send_text(await websocket.receive_text())
        await websocket.close()

    @app.api_route("/{path:path}", methods=["GET", "POST", "DELETE"])
    async def observe(path: str, request: Request):
        body = await request.body()
        result = {
            "path": path,
            "size": len(body),
            "forwarded_for": request.headers.get("x-forwarded-for"),
            "forwarded_proto": request.headers.get("x-forwarded-proto"),
        }
        received.append(result)
        return result

    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, log_level="error", access_log=False))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + 10
        while not server.started and thread.is_alive() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert server.started, "Disposable proxy upstream failed to start"
        yield port, received
    finally:
        server.should_exit = True
        thread.join(timeout=10)
        sock.close()
        assert not thread.is_alive(), "Proxy upstream did not stop"


@contextmanager
def running_nginx(binary, root, files, manifest, upstream_port):
    port = free_port()
    addresses = {
        manifest.scheduler.public_ip: "127.0.0.1",
        manifest.scheduler.private_ip: "127.0.0.2",
        manifest.gateway.public_ip: "127.0.0.3",
        manifest.gateway.private_ip: "127.0.0.4",
    }
    fragments = [
        files[name]
        for name in (
            "scheduler/headscale-nginx.conf",
            "scheduler/scheduler-nginx.conf",
            "gateway/transfer-gateway-nginx.conf",
        )
    ]
    replacements = {
        **{
            f"listen {address}:443 ssl": f"listen {alias}:{port} ssl"
            for address, alias in addresses.items()
        },
        "http://127.0.0.1:8080": f"http://127.0.0.1:{upstream_port}",
        "http://127.0.0.1:8081": f"http://127.0.0.1:{upstream_port}",
    }
    # Production address validation remains strict. Only this local runtime fixture
    # remaps explicit listeners, preserving their public/private separation.
    config = nginx_config(root, [substitute(source, replacements) for source in fragments])
    run(binary, "-p", str(root), "-c", str(config), "-t")
    with (root / "process.log").open("w+") as log:
        process = subprocess.Popen(
            [binary, "-p", str(root), "-c", str(config), "-g", "daemon off;"],
            stdout=log,
            stderr=log,
        )
        try:
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                assert process.poll() is None, "nginx exited before readiness"
                try:
                    with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                        break
                except OSError:
                    time.sleep(0.01)
            else:
                raise AssertionError("nginx did not become ready")
            yield {alias: f"https://{alias}:{port}" for alias in addresses.values()}
        finally:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=10)


def trust_local_certificate(root):
    context = ssl.create_default_context(cafile=str(root / "server.pem"))
    # Fixture cert names localhost/127.0.0.1; aliases represent separate interfaces.
    context.check_hostname = False
    return context
