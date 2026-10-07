"""Linux-only socket adapters for real userspace WireGuard + native SSH/Rclone tests."""

import os
import platform
import shutil
import subprocess
from pathlib import Path
from urllib.parse import urlparse

import pytest
from fl_common.errors import PlatformError
from fl_common.files import atomic_write
from fl_common.process import run_command


class SocketAdapters:
    def __init__(self, root):
        if platform.system() != "Linux":
            pytest.skip("Userspace native-socket adapters require Linux LD_PRELOAD")
        compiler = shutil.which("gcc")
        assert compiler, "Linux network acceptance requires gcc"
        self.root = root
        for name in ("connect", "addresses"):
            source = Path(__file__).with_suffix("") / f"{name}.c"
            subprocess.run(
                [
                    compiler,
                    "-shared",
                    "-fPIC",
                    "-Wall",
                    "-Wextra",
                    "-Werror",
                    str(source),
                    "-ldl",
                    "-o",
                    str(root / f"{name}.so"),
                ],
                capture_output=True,
                check=True,
                timeout=30,
            )

    def runner(self, peer, trace):
        async def run(argv, *, env, timeout):  # noqa: ASYNC109 -- matches subprocess runner
            environment = {
                **env,
                "LD_PRELOAD": str(self.root / "connect.so"),
                "FL_TEST_CONNECT_PROXY_PORT": str(urlparse(peer.proxy).port),
                "FL_TEST_CONNECT_TRACE": str(trace),
            }
            try:
                return await run_command(argv, env=environment, timeout=min(timeout, 30))
            except PlatformError as error:
                atomic_write(
                    trace.with_suffix(".error"), str(error.details.get("stderr", "")).encode()
                )
                raise

        return run

    def server_environment(self, source, destination, source_port, destination_port):
        return {
            **os.environ,
            "LD_PRELOAD": str(self.root / "addresses.so"),
            "FL_TEST_PROXY_SOURCE": source,
            "FL_TEST_PROXY_DESTINATION": destination,
            "FL_TEST_PROXY_SOURCE_PORT": source_port,
            "FL_TEST_PROXY_DESTINATION_PORT": destination_port,
        }
