"""Disposable inventory and native validator utilities; never touch installed services."""

import os
import subprocess
from pathlib import Path

import pytest
import yaml
from fl_common.files import atomic_write
from fl_common.ssh import create_identity
from fl_deploy.manifest import Deployment
from fl_deploy.native import quoted

from tests.dashboard_https import certificate

TEMPLATES = Path(__file__).resolve().parents[1] / "services"


def inventory(root, *, license_relay=False):
    data = yaml.safe_load((TEMPLATES / "deployment/example.yaml").read_text())
    data["gateway"]["host_key"] = create_identity(root / "host.key")
    if license_relay:
        data["license_relay"] = {
            "private_ip": "100.64.0.30",
            "backend_ip": "192.0.2.30",
            "hostname": "vivado-license.example.edu",
            "manager_port": 2100,
            "vendor_port": 2101,
        }
    return data


def require_native_tools():
    nginx = os.environ.get("FL_TEST_NGINX")
    if not nginx:
        pytest.skip("Set FL_TEST_NGINX, FL_TEST_HAPROXY and FL_TEST_HEADSCALE on Linux")
    tools = [nginx, os.environ.get("FL_TEST_HAPROXY"), os.environ.get("FL_TEST_HEADSCALE")]
    assert all(tool and Path(tool).is_file() for tool in tools), "Native validators are missing"
    return tools


def run(*args, env=None):
    result = subprocess.run(args, env=env, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
    return result.stdout


def certificates(root, manifest: Deployment):
    certificate(root)
    for host in (
        manifest.scheduler.hostname,
        manifest.scheduler.agent_hostname,
        manifest.scheduler.headscale_hostname,
        manifest.gateway.hostname,
        manifest.gateway.private_hostname,
    ):
        path = Path(manifest.certificate_root) / host
        path.mkdir(parents=True, mode=0o700)
        atomic_write(path / "fullchain.pem", (root / "server.pem").read_bytes())
        atomic_write(path / "privkey.pem", (root / "server.key").read_bytes())


def nginx_config(root, fragments):
    for directory in ("body", "proxy", "fastcgi", "uwsgi", "scgi", "var/log/nginx"):
        (root / directory).mkdir(parents=True, mode=0o700)
    path = root / "nginx.conf"
    atomic_write(
        path,
        (
            f"pid {quoted(str(root / 'nginx.pid'))};\n"
            f"error_log {quoted(str(root / 'nginx.log'))} warn;\n"
            "events { worker_connections 256; }\nhttp {\n"
            "access_log off;\n"
            f"client_body_temp_path {quoted(str(root / 'body'))};\n"
            f"proxy_temp_path {quoted(str(root / 'proxy'))};\n"
            f"fastcgi_temp_path {quoted(str(root / 'fastcgi'))};\n"
            f"uwsgi_temp_path {quoted(str(root / 'uwsgi'))};\n"
            f"scgi_temp_path {quoted(str(root / 'scgi'))};\n" + "\n".join(fragments) + "\n}\n"
        ).encode(),
    )
    return path
