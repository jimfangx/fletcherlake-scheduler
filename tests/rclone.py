"""Disposable SSH gateway for integration tests against an explicitly supplied Rclone."""

import getpass
import json
import os
import shutil
import subprocess
import time
from pathlib import Path

import pytest
from fl_common.models.transfer import TransferEndpoint
from fl_common.ssh import create_identity
from fl_gateway.store import GatewayStore

from tests.headscale import free_port


@pytest.fixture
def rclone_gateway(tmp_path):
    selected = os.environ.get("FL_TEST_RCLONE")
    if not selected:
        pytest.skip("Set FL_TEST_RCLONE to a downloaded official rclone binary")
    binary = Path(selected).resolve()
    assert binary.is_file()
    sshd = shutil.which("sshd") or "/usr/sbin/sshd"
    store = GatewayStore(tmp_path / "gateway with spaces")
    host_key = tmp_path / "host-key"
    public = create_identity(host_key)
    port = free_port()
    config = tmp_path / "sshd.conf"
    config.write_text(
        f"Port {port}\nListenAddress 127.0.0.1\nHostKey {host_key}\n"
        f"PidFile {tmp_path / 'sshd.pid'}\nUsePAM no\nPasswordAuthentication no\n"
        # /tmp is a writable ancestor outside the real account's home. Production
        # keeps StrictModes enabled with authorized keys under a protected home.
        "KbdInteractiveAuthentication no\nPubkeyAuthentication yes\nStrictModes no\n"
        f"AuthorizedKeysFile {json.dumps(str(store.root / 'authorized_keys'))}\n"
        "AllowTcpForwarding no\nX11Forwarding no\nPermitTTY no\n"
        f"AllowUsers {getpass.getuser()}\nLogLevel ERROR\n"
        "Subsystem sftp internal-sftp\n"
    )
    logs = (tmp_path / "sshd.log").open("w+")
    process = subprocess.Popen([sshd, "-D", "-e", "-f", str(config)], stdout=logs, stderr=logs)
    try:
        time.sleep(0.15)
        if process.poll() is not None:
            logs.seek(0)
            pytest.fail("Disposable SSH server exited: " + logs.read())
        yield (
            store,
            TransferEndpoint(
                host="127.0.0.1", username=getpass.getuser(), port=port, host_key=public
            ),
            binary,
        )
    finally:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
        logs.close()
