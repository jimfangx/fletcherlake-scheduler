"""Non-secret deployment inventory and native configuration injection boundaries."""

import re
from ipaddress import IPv4Address, IPv4Network
from pathlib import PurePosixPath
from typing import Annotated, Self

from fl_common.models.base import Schema
from fl_common.ssh import public_key
from pydantic import AfterValidator, Field, model_validator

TAILNET = IPv4Network("100.64.0.0/10")


def hostname(value: str) -> str:
    if (
        len(value) > 253
        or "." not in value
        or any(
            not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label)
            for label in value.split(".")
        )
    ):
        raise ValueError("Expected a lowercase fully qualified DNS hostname")
    return value


def linux_path(value: str) -> str:
    path = PurePosixPath(value)
    if (
        not path.is_absolute()
        or ".." in path.parts
        or any(ord(char) < 32 or ord(char) > 126 or char in "$\\" for char in value)
    ):
        raise ValueError("Expected an absolute Linux path without controls, variables or '..'")
    if value != value.rstrip():
        raise ValueError("Linux paths cannot end in whitespace")
    return value


def unicast(value: IPv4Address) -> IPv4Address:
    if value.is_unspecified or value.is_multicast or value.is_loopback or value.is_reserved:
        raise ValueError("Expected a concrete unicast IPv4 address")
    return value


def private_address(value: IPv4Address) -> IPv4Address:
    if (
        value not in TAILNET
        or value == TAILNET.network_address
        or value == TAILNET.broadcast_address
    ):
        raise ValueError("Private listeners must use allocated Headscale IPv4 addresses")
    return value


def public_address(value: IPv4Address) -> IPv4Address:
    if value in TAILNET:
        raise ValueError("Public listeners must use a separate host address")
    return unicast(value)


Host = Annotated[str, AfterValidator(hostname)]
LinuxPath = Annotated[str, AfterValidator(linux_path)]
PrivateIP = Annotated[IPv4Address, AfterValidator(private_address)]
PublicIP = Annotated[IPv4Address, AfterValidator(public_address)]
Port = Annotated[int, Field(ge=1, le=65535)]


class Scheduler(Schema):
    public_ip: PublicIP
    private_ip: PrivateIP
    hostname: Host
    agent_hostname: Host
    headscale_hostname: Host


class Gateway(Schema):
    public_ip: PublicIP
    private_ip: PrivateIP
    hostname: Host
    private_hostname: Host
    host_key: Annotated[str, AfterValidator(public_key)]


class LicenseRelay(Schema):
    private_ip: PrivateIP
    backend_ip: Annotated[IPv4Address, AfterValidator(unicast)]
    hostname: Host
    manager_port: Port = 2100
    vendor_port: Port = 2101

    @model_validator(mode="after")
    def fixed_ports(self) -> Self:
        if self.manager_port == self.vendor_port or self.backend_ip in TAILNET:
            raise ValueError("Use distinct fixed license ports and the BWRC backend address")
        return self


class Deployment(Schema):
    scheduler: Scheduler
    gateway: Gateway
    license_relay: LicenseRelay | None = None
    install_root: LinuxPath = "/opt/fl"
    certificate_root: LinuxPath = "/etc/letsencrypt/live"
    headscale_binary: LinuxPath = "/usr/local/bin/headscale"

    @model_validator(mode="after")
    def distinct_hosts(self) -> Self:
        addresses = [
            self.scheduler.public_ip,
            self.scheduler.private_ip,
            self.gateway.public_ip,
            self.gateway.private_ip,
        ]
        hosts = [
            self.scheduler.hostname,
            self.scheduler.agent_hostname,
            self.scheduler.headscale_hostname,
            self.gateway.hostname,
            self.gateway.private_hostname,
        ]
        if self.license_relay is not None:
            addresses.append(self.license_relay.private_ip)
            hosts.append(self.license_relay.hostname)
        if len(set(addresses)) != len(addresses) or len(set(hosts)) != len(hosts):
            raise ValueError("Linux roles need distinct addresses and service names")
        return self
