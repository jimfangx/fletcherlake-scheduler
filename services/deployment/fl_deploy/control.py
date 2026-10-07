"""Keep coordinator DNS, ACL ports and pinned transfer endpoints consistent."""

import json
from pathlib import Path
from typing import Any

import yaml
from fl_common.models.transfer import TransferEndpoint

from .manifest import Deployment
from .native import substitute


def json_text(value: Any) -> str:
    return json.dumps(value, indent=2) + "\n"


def render_control(manifest: Deployment, templates: Path) -> dict[str, str]:
    scheduler, gateway, relay = manifest.scheduler, manifest.gateway, manifest.license_relay
    coordinator = yaml.safe_load((templates / "headscale/config.yaml").read_text())
    coordinator["server_url"] = f"https://{scheduler.headscale_hostname}"
    records = [
        (scheduler.agent_hostname, scheduler.private_ip),
        (gateway.private_hostname, gateway.private_ip),
    ]
    if relay is not None:
        records.append((relay.hostname, relay.private_ip))
    coordinator["dns"]["extra_records"] = [
        {"name": host, "type": "A", "value": str(address)} for host, address in records
    ]
    policy = json.loads((templates / "headscale/policy.json").read_text())
    replacements: dict[str, str] = {}
    if relay is None:
        policy["tagOwners"].pop("tag:license-relay", None)
    else:
        replacements["tag:license-relay:2100,2101"] = (
            f"tag:license-relay:{relay.manager_port},{relay.vendor_port}"
        )
    for rule in policy["acls"]:
        rule["dst"] = [
            substitute(destination, replacements) if replacements else destination
            for destination in rule["dst"]
            if relay is not None or not destination.startswith("tag:license-relay:")
        ]
    policy["acls"] = [rule for rule in policy["acls"] if rule["dst"]]
    result = {
        "scheduler/headscale.yaml": yaml.safe_dump(coordinator, sort_keys=False),
        "scheduler/policy.json": json_text(policy),
    }
    for name, host in (("public", gateway.hostname), ("private", str(gateway.private_ip))):
        endpoint = TransferEndpoint(
            host=host,
            host_key=gateway.host_key,
        )
        result[f"gateway/{name}-endpoint.json"] = json_text(endpoint.model_dump(mode="json"))
    return result
