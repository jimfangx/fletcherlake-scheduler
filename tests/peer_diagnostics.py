"""Retain public transport state without copying private keys or control credentials."""

import json
import subprocess

from fl_common.files import atomic_write

PEER_FIELDS = (
    "HostName",
    "TailscaleIPs",
    "Relay",
    "CurAddr",
    "RxBytes",
    "TxBytes",
    "Online",
    "Active",
)


def retain_transport_state(peer, path):
    try:
        status = json.loads(peer.command("status", "--json"))
        mapping = json.loads(peer.command("debug", "netmap"))
        if not isinstance(mapping, dict) or not isinstance(status.get("Self"), dict):
            raise ValueError("Peer has no network map")
        report = {
            "backend_state": status["BackendState"],
            "self": {key: status["Self"].get(key) for key in PEER_FIELDS},
            "peers": [
                {key: target.get(key) for key in PEER_FIELDS}
                for target in (status.get("Peer") or {}).values()
            ],
            # The full netmap contains machine/node keys and user profiles. Never save it.
            "packet_filter": mapping["PacketFilter"],
            "packet_filter_rules": mapping["PacketFilterRules"],
        }
    except (AssertionError, KeyError, ValueError, OSError, subprocess.TimeoutExpired) as error:
        # A failed diagnostic must not hide the original test failure or echo daemon output.
        report = {"diagnostic_error": type(error).__name__}
    atomic_write(path, json.dumps(report, indent=2).encode())
