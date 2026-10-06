"""Bounded local TLS listeners for actual browser redirects between two origins."""

import ipaddress
import socket
import threading
import time
from contextlib import contextmanager
from datetime import timedelta

import uvicorn
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
from fl_common.files import atomic_write
from fl_common.models.base import utcnow


def certificate(root, *, ip_addresses=("127.0.0.1",)):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")])
    now = utcnow()
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=1))
        .not_valid_after(now + timedelta(hours=1))
        .add_extension(
            x509.SubjectAlternativeName(
                [x509.IPAddress(ipaddress.ip_address(value)) for value in ip_addresses]
                + [x509.DNSName("localhost")]
            ),
            critical=False,
        )
        .sign(key, hashes.SHA256())
    )
    atomic_write(
        root / "server.key",
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        ),
    )
    atomic_write(root / "server.pem", cert.public_bytes(serialization.Encoding.PEM))


@contextmanager
def serve_https(factory, root, hostname="127.0.0.1"):
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    origin = f"https://{hostname}:{sock.getsockname()[1]}"
    thread = None
    server = None
    try:
        app = factory(origin)
        server = uvicorn.Server(
            uvicorn.Config(
                app,
                log_level="error",
                access_log=False,
                ssl_keyfile=str(root / "server.key"),
                ssl_certfile=str(root / "server.pem"),
            )
        )
        thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
        thread.start()
        deadline = time.monotonic() + 10
        while not server.started and thread.is_alive() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert server.started, "Browser fixture HTTPS server did not start"
        yield origin
    finally:
        if server:
            server.should_exit = True
        if thread:
            thread.join(10)
        sock.close()
        assert thread is None or not thread.is_alive(), "HTTPS fixture did not stop"
