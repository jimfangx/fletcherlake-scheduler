"""Render hardened Linux units, retaining protected operator-owned environment files."""

from pathlib import Path

from .manifest import Deployment
from .native import substitute, systemd

PRIVATE_ORDERING = """[Unit]
After=network-online.target tailscaled.service
Wants=network-online.target tailscaled.service

[Service]
Restart=on-failure
RestartSec=5
"""


def render_units(manifest: Deployment, templates: Path) -> dict[str, str]:
    result = {}
    for role, destination, name in (
        ("scheduler", "scheduler", "fl-scheduler.service"),
        ("headscale", "scheduler", "headscale.service"),
        ("transfer-gateway", "gateway", "fl-transfer-gateway.service"),
    ):
        source = (templates / role / name).read_text()
        if role == "headscale":
            source = source.replace(
                "/usr/local/bin/headscale", "/usr/bin/env -- " + systemd(manifest.headscale_binary)
            )
        else:
            executable = "fl-scheduler" if role == "scheduler" else "fl-transfer-gateway"
            source = substitute(
                source,
                {
                    # systemd rejects quotes inside the executable name. env
                    # executes the absolute, quoted argument without a shell.
                    f"/opt/fl/.pixi/envs/default/bin/{executable}": (
                        "/usr/bin/env -- "
                        + systemd(f"{manifest.install_root}/.pixi/envs/default/bin/{executable}")
                    ),
                    "WorkingDirectory=/opt/fl": (
                        # This directive takes one literal path, not shell words.
                        "WorkingDirectory=" + manifest.install_root.replace("%", "%%")
                    ),
                },
            )
        result[f"{destination}/{name}"] = source
    private_services = [("scheduler", "nginx"), ("gateway", "nginx")]
    if manifest.license_relay is not None:
        private_services.append(("license-relay", "haproxy"))
    for role, service in private_services:
        result[f"{role}/{service}.service.d/10-headscale.conf"] = PRIVATE_ORDERING
    return result
