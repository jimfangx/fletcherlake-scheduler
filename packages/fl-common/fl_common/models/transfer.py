"""Scoped transport metadata. No transfer credential belongs in JobConfig or ClusterConfig."""

import ipaddress
import re
from typing import Literal
from uuid import UUID

from pydantic import AwareDatetime, Field, field_validator, model_validator

from fl_common.ssh import public_key

from .artifact import ArtifactRef
from .base import Schema


class TransferEndpoint(Schema):
    host: str
    port: int = Field(default=22, ge=1, le=65535)
    username: str = Field(default="fl-transfer", pattern=r"^[a-z_][a-z0-9_-]{0,31}$")
    host_key: str
    data_port_first: int = Field(default=5000, ge=1024, le=65535)
    data_port_last: int = Field(default=5099, ge=1024, le=65535)

    @field_validator("host")
    @classmethod
    def safe_host(cls, value: str) -> str:
        try:
            return str(ipaddress.ip_address(value))
        except ValueError:
            if len(value) > 253 or not re.fullmatch(
                r"[a-zA-Z0-9](?:[a-zA-Z0-9.-]*[a-zA-Z0-9])?", value
            ):
                raise ValueError("Expected a DNS hostname or literal IP address") from None
            if any(not label or len(label) > 63 for label in value.split(".")):
                raise ValueError("Invalid DNS label") from None
            return value

    @field_validator("host_key")
    @classmethod
    def valid_key(cls, value: str) -> str:
        return public_key(value)

    @model_validator(mode="after")
    def port_range(self) -> "TransferEndpoint":
        if self.data_port_last < self.data_port_first + 7:
            raise ValueError("BBCP needs a range of at least eight data ports")
        return self


class TransferGrant(Schema):
    transfer_id: UUID
    job_id: UUID
    direction: Literal["upload", "download"]
    public_key: str
    token_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    files: list[ArtifactRef] = Field(min_length=1, max_length=6)
    expires_at: AwareDatetime
    retains_until: AwareDatetime
    source_id: UUID | None = None
    source_networks: list[str] = Field(default_factory=list, max_length=8)
    public_download: bool = False

    @field_validator("public_key")
    @classmethod
    def valid_key(cls, value: str) -> str:
        return public_key(value)

    @model_validator(mode="after")
    def scope(self) -> "TransferGrant":
        if self.retains_until < self.expires_at:
            raise ValueError("Payload retention cannot end before credentials expire")
        if len({ref.kind for ref in self.files}) != len(self.files):
            raise ValueError("Transfer kinds must be unique")
        if (self.source_id is not None) != (self.direction == "download"):
            raise ValueError("Only download grants name a verified source transfer")
        for network in self.source_networks:
            ipaddress.ip_network(network, strict=True)
        if self.public_download and (self.direction != "download" or self.source_networks):
            raise ValueError("Public read scopes name no private source networks")
        if self.direction == "download" and not self.public_download and not self.source_networks:
            raise ValueError("Private download grants require explicit source networks")
        return self


class TransferStatus(Schema):
    grant: TransferGrant
    state: Literal["OPEN", "VERIFIED", "REVOKED"]
