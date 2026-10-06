"""Disposable coordinator, embedded relay and shipped ACL configuration."""

from pathlib import Path

import yaml


def write_config(root, port, stun_port, grpc_port):
    # Actual local fallback relay; peers use Tailscale's test-only HTTP DERP knob.
    # This avoids external relay traffic while preserving encrypted peer packets.
    derp = root / "derp.yaml"
    derp.write_text(
        yaml.safe_dump(
            {
                "regions": {
                    1: {
                        "regionid": 1,
                        "regioncode": "test",
                        "regionname": "Local test",
                        "nodes": [
                            {
                                "name": "test",
                                "regionid": 1,
                                "hostname": "127.0.0.1",
                                "ipv4": "127.0.0.1",
                                "derpport": port,
                                "stunport": stun_port,
                            }
                        ],
                    }
                }
            }
        )
    )
    config = yaml.safe_load(Path("services/headscale/config.yaml").read_text())
    config.update(
        {
            "server_url": f"http://127.0.0.1:{port}",
            "listen_addr": f"127.0.0.1:{port}",
            "metrics_listen_addr": "",
            "grpc_listen_addr": f"127.0.0.1:{grpc_port}",
            "unix_socket": str(root / "headscale.sock"),
            "noise": {"private_key_path": str(root / "noise.key")},
            "database": {"type": "sqlite", "sqlite": {"path": str(root / "headscale.db")}},
            "derp": {
                "server": {
                    "enabled": True,
                    "region_id": 1,
                    "region_code": "test",
                    "region_name": "Local test",
                    "verify_clients": True,
                    "stun_listen_addr": f"127.0.0.1:{stun_port}",
                    "private_key_path": str(root / "derp.key"),
                    "automatically_add_embedded_derp_region": False,
                },
                "urls": [],
                "paths": [str(derp)],
                "auto_update_enabled": False,
            },
            "policy": {
                "mode": "file",
                "path": str(Path("services/headscale/policy.json").resolve()),
            },
        }
    )
    path = root / "config.yaml"
    path.write_text(yaml.safe_dump(config))
    return path
