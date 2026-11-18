"""Partition real Headscale/Tailscale acceptance while keeping every case in CI."""

from collections.abc import Collection, Mapping

NETWORK_FIXTURES = {"headscale_process", "headscale_server", "tailscale_peers", "private_stack"}


def network_group(fixtures: Collection[str], parameters: Mapping[str, object]) -> str | None:
    if NETWORK_FIXTURES.isdisjoint(fixtures):
        return "network_public" if "rclone_gateway" in fixtures else None
    return "network_relay" if parameters.get("tailscale_peers") is True else "network_normal"
