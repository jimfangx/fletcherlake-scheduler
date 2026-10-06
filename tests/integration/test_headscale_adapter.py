"""Real and injected Headscale REST contract checks; bootstrap secrets never grant admin access."""

import json
from datetime import timedelta

import httpx
import pytest
from fl_common.errors import PlatformError
from fl_common.models.base import utcnow
from fl_scheduler.network.headscale import Headscale
from pydantic import SecretStr


async def test_real_headscale_creates_scoped_key_and_expires_it(headscale_server):
    url, api_key = headscale_server
    async with httpx.AsyncClient() as client:
        provider = Headscale(url, "https://headscale.test", SecretStr(api_key), client)
        key = await provider.create_cluster_key(utcnow() + timedelta(minutes=10))
        assert key.key.get_secret_value().startswith("hskey-auth-")
        assert await provider.key_nodes(key.key_id) == ()
        await provider.expire_key(key.key_id)
        response = await client.get(
            url + "/api/v1/preauthkey", headers={"Authorization": "Bearer " + api_key}
        )
        response.raise_for_status()
        value = next(value for value in response.json()["preAuthKeys"] if value["id"] == key.key_id)
        assert value["reusable"] is False and value["aclTags"] == ["tag:cluster"]
        assert value["key"] != key.key.get_secret_value()


async def test_headscale_wire_contract_and_foreign_node_rejection():
    calls = []
    expiry = utcnow() + timedelta(minutes=10)
    node_key = "nodekey:" + "a" * 64
    node = {
        "id": "5",
        "nodeKey": node_key,
        "preAuthKey": {"id": "7"},
        "tags": ["tag:cluster"],
        "ipAddresses": ["100.64.0.5"],
    }

    def handle(request):
        calls.append(request)
        if request.method == "GET":
            return httpx.Response(200, json={"nodes": [node]})
        payload = json.loads(request.content)
        assert payload["reusable"] is False and payload["ephemeral"] is False
        assert payload["aclTags"] == ["tag:cluster"]
        return httpx.Response(
            200,
            json={
                "preAuthKey": {
                    "id": "7",
                    "key": "private-key",
                    "expiration": expiry.isoformat(),
                    "reusable": False,
                    "ephemeral": False,
                    "aclTags": ["tag:cluster"],
                }
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        provider = Headscale(
            "http://127.0.0.1:8081", "https://headscale.test", SecretStr("admin-key"), client
        )
        key = await provider.create_cluster_key(expiry)
        joined = await provider.joined_node(key.key_id, node_key)
        assert joined.addresses == ("100.64.0.5",)
        node["preAuthKey"]["id"] = "8"
        with pytest.raises(PlatformError, match="not joined with this enrollment key"):
            await provider.joined_node(key.key_id, node_key)
        node["preAuthKey"]["id"] = "7"
        node["tags"] = ["tag:cluster", "tag:scheduler"]
        with pytest.raises(PlatformError, match="not joined with this enrollment key"):
            await provider.joined_node(key.key_id, node_key)
        node["tags"] = ["tag:cluster"]
        node["ipAddresses"] = ["203.0.113.5"]
        with pytest.raises(PlatformError, match="invalid node metadata"):
            await provider.joined_node(key.key_id, node_key)
    assert all(request.headers["Authorization"] == "Bearer admin-key" for request in calls)


@pytest.mark.parametrize(
    "url",
    [
        "http://headscale.example.edu",
        "http://100.64.0.1",
        "ftp://127.0.0.1",
        "https://user:secret@example.edu",
        "https://example.edu/api",
    ],
)
async def test_headscale_admin_url_rejects_unsafe_origins(url):
    async with httpx.AsyncClient() as client:
        with pytest.raises(ValueError):
            Headscale(url, "https://headscale.test", SecretStr("secret"), client)
