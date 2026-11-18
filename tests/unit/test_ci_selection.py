import pytest

from tests.ci_selection import network_group


@pytest.mark.parametrize(
    "fixtures,parameters,expected",
    [
        (["config", "scheduler_db", "connected_agents"], {}, None),
        (["rclone_gateway"], {}, "network_public"),
        (["headscale_server"], {}, "network_normal"),
        (["private_stack", "headscale_process"], {}, "network_normal"),
        (["tailscale_peers"], {"tailscale_peers": False}, "network_normal"),
        (["tailscale_peers"], {"tailscale_peers": True}, "network_relay"),
    ],
)
def test_network_acceptance_is_partitioned_by_required_fixtures(fixtures, parameters, expected):
    assert network_group(fixtures, parameters) == expected
