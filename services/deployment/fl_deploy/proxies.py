"""Render explicitly bound TLS/SSH proxies and fixed-port license forwarding."""

from pathlib import Path

from .manifest import Deployment
from .native import quoted, substitute


def nginx(source: str, manifest: Deployment, substitutions: dict[str, str]) -> str:
    source = substitute(source, substitutions)
    lines = []
    for line in source.splitlines():
        stripped = line.strip()
        if stripped.startswith(("ssl_certificate ", "ssl_certificate_key ")):
            directive, path = stripped[:-1].split(" ", 1)
            path = path.replace("/etc/letsencrypt/live", manifest.certificate_root, 1)
            line = f"    {directive} {quoted(path)};"
        lines.append(line)
    return "\n".join(lines) + "\n"


def render_proxies(manifest: Deployment, templates: Path) -> dict[str, str]:
    scheduler, gateway, relay = manifest.scheduler, manifest.gateway, manifest.license_relay
    result = {}
    replacements = {
        "headscale": {
            "PUBLIC_IP": str(scheduler.public_ip),
            "headscale.example.edu": scheduler.headscale_hostname,
        },
        "scheduler": {
            "PUBLIC_IP": str(scheduler.public_ip),
            "SCHEDULER_HEADSCALE_IP": str(scheduler.private_ip),
            "agent.scheduler.example.edu": scheduler.agent_hostname,
            "scheduler.example.edu": scheduler.hostname,
        },
        "transfer-gateway": {
            "TRANSFER_PUBLIC_IP": str(gateway.public_ip),
            "GATEWAY_HEADSCALE_IP": str(gateway.private_ip),
            "gateway.internal.example.edu": gateway.private_hostname,
            "transfer.example.edu": gateway.hostname,
        },
    }
    for role, substitutions in replacements.items():
        source = (templates / role / "nginx.conf").read_text()
        # Headscale and scheduler run on one host; the gateway runs separately.
        destination = "gateway" if role == "transfer-gateway" else "scheduler"
        result[f"{destination}/{role}-nginx.conf"] = nginx(source, manifest, substitutions)
    source = (templates / "transfer-gateway/sshd.conf").read_text()
    result["gateway/sshd.conf"] = source.replace(
        "TRANSFER_PUBLIC_IP", str(gateway.public_ip)
    ).replace("GATEWAY_HEADSCALE_IP", str(gateway.private_ip))
    if relay is not None:
        source = (templates / "license-relay/haproxy.cfg").read_text()
        result["license-relay/haproxy.cfg"] = substitute(
            source,
            {
                "RELAY_HEADSCALE_IP": str(relay.private_ip),
                "BWRC_LICENSE_IP": str(relay.backend_ip),
                ":2100": f":{relay.manager_port}",
                ":2101": f":{relay.vendor_port}",
            },
        )
    return result
