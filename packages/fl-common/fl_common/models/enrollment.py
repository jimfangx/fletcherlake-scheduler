"""Bootstrap contracts. Enrollment secrets are separate from cluster inventory."""

from uuid import UUID

from pydantic import AwareDatetime, Field, SecretStr, field_validator

from fl_common.network import https_origin

from .base import Schema
from .cluster import ClusterConfig


class EnrollmentIssued(Schema):
    enrollment_id: UUID
    cluster_id: UUID
    token: SecretStr
    expires_at: AwareDatetime

    def wire(self) -> dict[str, object]:
        body = self.model_dump(mode="json")
        body["token"] = self.token.get_secret_value()
        return body


class EnrollmentClaim(Schema):
    bootstrap_secret: SecretStr = Field(min_length=32, max_length=128)


class EnrollmentGrant(Schema):
    cluster_id: UUID
    headscale_url: str
    agent_origin: str
    auth_key: SecretStr
    expires_at: AwareDatetime

    @field_validator("headscale_url", "agent_origin")
    @classmethod
    def valid_origin(cls, value: str) -> str:
        return https_origin(value)

    def wire(self) -> dict[str, object]:
        body = self.model_dump(mode="json")
        body["auth_key"] = self.auth_key.get_secret_value()
        return body


class EnrollmentRegistration(EnrollmentClaim):
    config: ClusterConfig
    node_key: str = Field(pattern=r"^nodekey:[0-9a-f]{64}$")
    agent_token: SecretStr = Field(min_length=32, max_length=256)
