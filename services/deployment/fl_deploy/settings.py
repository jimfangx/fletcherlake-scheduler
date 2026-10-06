"""Non-secret service defaults to merge into protected operator environment files."""

from .manifest import Deployment
from .native import quoted


def environment(values: dict[str, str]) -> str:
    return "\n".join(f"{key}={quoted(value)}" for key, value in values.items()) + "\n"


def render_settings(manifest: Deployment) -> dict[str, str]:
    scheduler, gateway = manifest.scheduler, manifest.gateway
    return {
        "scheduler/environment.defaults": environment(
            {
                "FL_BIND_HOST": "127.0.0.1",
                "FL_BIND_PORT": "8080",
                "FL_PUBLIC_ORIGIN": f"https://{scheduler.hostname}",
                "FL_HEADSCALE_ADMIN_URL": "http://127.0.0.1:8081",
                "FL_HEADSCALE_LOGIN_URL": f"https://{scheduler.headscale_hostname}",
                "FL_AGENT_ORIGIN": f"https://{scheduler.agent_hostname}",
                "FL_TRANSFER_GATEWAY_ORIGIN": f"https://{gateway.private_hostname}",
                "FL_TRANSFER_PUBLIC_ORIGIN": f"https://{gateway.hostname}",
                "FL_TRANSFER_PRIVATE_ENDPOINT_FILE": "/etc/fl/private-endpoint.json",
                "FL_TRANSFER_PUBLIC_ENDPOINT_FILE": "/etc/fl/public-endpoint.json",
                "FL_DASHBOARD_DIR": f"{manifest.install_root}/services/dashboard/dist",
            }
        ),
        "gateway/environment.defaults": environment(
            {
                "FL_BIND_HOST": "127.0.0.1",
                "FL_BIND_PORT": "8081",
                "FL_GATEWAY_ROOT": "/var/lib/fl-transfer",
                "FL_GATEWAY_BBCP": manifest.bbcp_binary,
                "FL_GATEWAY_CONTROL_SECRET_FILE": "/var/lib/fl-transfer/control-secret",
                "FL_GATEWAY_DATA_PORT_FIRST": str(gateway.data_port_first),
                "FL_GATEWAY_DATA_PORT_LAST": str(gateway.data_port_last),
            }
        ),
    }
