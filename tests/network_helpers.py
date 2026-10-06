"""Deterministic Headscale provider for enrollment failure and retry tests."""

from fl_common.errors import PlatformError
from fl_scheduler.network.headscale import NetworkKey, NetworkNode
from pydantic import SecretStr


class MockNetwork:
    def __init__(self):
        self.keys = {}
        self.nodes = {}
        self.create_calls = 0
        self.deleted = []
        self.expired = []
        self.unavailable = False
        self.before_create = None

    async def create_cluster_key(self, expires_at):
        if self.before_create:
            await self.before_create()
        if self.unavailable:
            raise PlatformError("NETWORK_UNAVAILABLE", "Headscale unavailable")
        self.create_calls += 1
        key = NetworkKey(
            str(self.create_calls), SecretStr(f"private-join-key-{self.create_calls}"), expires_at
        )
        self.keys[key.key_id] = key
        return key

    def join(self, key_id):
        node_key = "nodekey:" + int(key_id).to_bytes(32).hex()
        node = NetworkNode(key_id, node_key, (f"100.64.0.{key_id}",))
        self.nodes[node_key] = (key_id, node)
        return node

    async def joined_node(self, key_id, node_key):
        if node_key not in self.nodes or self.nodes[node_key][0] != key_id:
            raise PlatformError("NODE_IDENTITY", "Node was not joined with this enrollment key")
        return self.nodes[node_key][1]

    async def expire_key(self, key_id):
        if self.unavailable:
            raise PlatformError("NETWORK_UNAVAILABLE", "Headscale unavailable")
        self.expired.append(key_id)

    async def key_nodes(self, key_id):
        return tuple(
            node.node_id for enrolled_key, node in self.nodes.values() if enrolled_key == key_id
        )

    async def delete_node(self, node_id):
        if self.unavailable:
            raise PlatformError("NETWORK_UNAVAILABLE", "Headscale unavailable")
        self.deleted.append(node_id)
        self.nodes = {
            key: value for key, value in self.nodes.items() if value[1].node_id != node_id
        }
