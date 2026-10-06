"""Headscale 0.29 REST adapter, based on its versioned protobuf/OpenAPI contracts."""

import ipaddress
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol
from urllib.parse import urlsplit

import httpx
from fl_common.errors import PlatformError
from fl_common.models.base import utcnow
from fl_common.network import https_origin
from pydantic import SecretStr


@dataclass(frozen=True)
class NetworkKey:
    key_id: str
    key: SecretStr
    expires_at: datetime


@dataclass(frozen=True)
class NetworkNode:
    node_id: str
    node_key: str
    addresses: tuple[str, ...]


class NetworkControl(Protocol):
    async def create_cluster_key(self, expires_at: datetime) -> NetworkKey: ...

    async def joined_node(self, key_id: str, node_key: str) -> NetworkNode: ...

    async def expire_key(self, key_id: str) -> None: ...

    async def delete_node(self, node_id: str) -> None: ...

    async def key_nodes(self, key_id: str) -> tuple[str, ...]: ...


def numeric_id(value: object) -> str:
    result = str(value)
    if not result.isascii() or not result.isdecimal() or int(result) <= 0:
        raise ValueError("Headscale returned an invalid identifier")
    return result


class Headscale:
    def __init__(
        self, admin_url: str, login_url: str, api_key: SecretStr, client: httpx.AsyncClient
    ) -> None:
        url = urlsplit(admin_url)
        if url.scheme == "http":
            if not url.hostname or not ipaddress.ip_address(url.hostname).is_loopback:
                raise ValueError("Unencrypted Headscale admin API must use a literal loopback IP")
        elif url.scheme != "https":
            raise ValueError("Headscale admin API requires HTTPS or loopback HTTP")
        if (
            not url.hostname
            or url.username
            or url.path not in {"", "/"}
            or url.query
            or url.fragment
        ):
            raise ValueError("Headscale admin URL must be a plain origin")
        self.admin_url, self.login_url = admin_url.rstrip("/"), https_origin(login_url)
        self.api_key, self.client = api_key, client

    async def _request(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        try:
            response = await self.client.request(
                method,
                self.admin_url + "/api/v1/" + path,
                headers={"Authorization": "Bearer " + self.api_key.get_secret_value()},
                timeout=15,
                follow_redirects=False,
                **kwargs,
            )
            if response.status_code == 404 and (method == "DELETE" or path == "preauthkey/expire"):
                return {}
            response.raise_for_status()
            body = response.json()
            if not isinstance(body, dict):
                raise ValueError("Malformed Headscale response")
            return body
        except (httpx.HTTPError, ValueError) as error:
            # Secret-bearing provider exceptions never cross the API boundary.
            raise PlatformError("NETWORK_UNAVAILABLE", "Headscale operation failed") from error

    async def create_cluster_key(self, expires_at: datetime) -> NetworkKey:
        body = await self._request(
            "POST",
            "preauthkey",
            json={
                "reusable": False,
                "ephemeral": False,
                "expiration": expires_at.isoformat(),
                "aclTags": ["tag:cluster"],
            },
        )
        try:
            value = body["preAuthKey"]
            expiration = datetime.fromisoformat(value["expiration"].replace("Z", "+00:00"))
            if (
                value["reusable"] is not False
                or value["ephemeral"] is not False
                or set(value["aclTags"]) != {"tag:cluster"}
                or expiration.tzinfo is None
                or not utcnow() < expiration <= expires_at
                or not isinstance(value["key"], str)
                or not value["key"]
            ):
                raise ValueError("Headscale key does not have the requested scope")
            return NetworkKey(numeric_id(value["id"]), SecretStr(value["key"]), expiration)
        except (KeyError, TypeError, ValueError) as error:
            raise PlatformError("NETWORK_PROTOCOL", "Headscale returned an invalid key") from error

    async def joined_node(self, key_id: str, node_key: str) -> NetworkNode:
        body = await self._request("GET", "node")
        try:
            nodes = [node for node in body.get("nodes", []) if node.get("nodeKey") == node_key]
            if len(nodes) != 1:
                raise PlatformError("NODE_NOT_JOINED", "Headscale has not registered this node")
            node = nodes[0]
            if numeric_id(node["preAuthKey"]["id"]) != numeric_id(key_id) or set(node["tags"]) != {
                "tag:cluster"
            }:
                raise PlatformError("NODE_IDENTITY", "Node was not joined with this enrollment key")
            addresses = tuple(str(ipaddress.ip_address(ip)) for ip in node["ipAddresses"])
            if not addresses or any(not tailnet_address(ip) for ip in addresses):
                raise ValueError("Node has no private infrastructure address")
            return NetworkNode(numeric_id(node["id"]), node_key, addresses)
        except (KeyError, TypeError, ValueError) as error:
            raise PlatformError(
                "NETWORK_PROTOCOL", "Headscale returned invalid node metadata"
            ) from error

    async def expire_key(self, key_id: str) -> None:
        await self._request("POST", "preauthkey/expire", json={"id": numeric_id(key_id)})

    async def delete_node(self, node_id: str) -> None:
        await self._request("DELETE", "node/" + numeric_id(node_id))

    async def key_nodes(self, key_id: str) -> tuple[str, ...]:
        body = await self._request("GET", "node")
        try:
            return tuple(
                numeric_id(node["id"])
                for node in body.get("nodes", [])
                if str(node.get("preAuthKey", {}).get("id")) == numeric_id(key_id)
            )
        except (KeyError, TypeError, ValueError) as error:
            raise PlatformError("NETWORK_PROTOCOL", "Headscale returned invalid nodes") from error


def tailnet_address(value: str) -> bool:
    address = ipaddress.ip_address(value)
    return address in ipaddress.ip_network("100.64.0.0/10") or address in ipaddress.ip_network(
        "fd7a:115c:a1e0::/48"
    )
