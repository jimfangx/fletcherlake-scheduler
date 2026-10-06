"""Retryable macOS enrollment shared by Python management and the CLI."""

import hashlib
import json
import os
import secrets
from pathlib import Path

import httpx
from fl_agent.configuration import write_config
from fl_agent.credentials import AgentCredentials, load_credentials
from fl_agent.locks import ExclusiveLock
from fl_common.errors import PlatformError
from fl_common.files import atomic_write, fsync_directory
from fl_common.models import ClusterConfig
from fl_common.models.base import Schema
from fl_common.models.enrollment import EnrollmentGrant
from fl_common.models.scheduler import ClusterSnapshot
from fl_common.network import https_origin
from pydantic import SecretStr

from .networking import TailscaleClient


class SetupReceipt(Schema):
    scheduler: str
    token: SecretStr
    bootstrap_secret: SecretStr
    agent_token: SecretStr
    inventory_digest: str
    grant: EnrollmentGrant | None = None
    node_key: str | None = None

    def protected_bytes(self) -> bytes:
        body = self.model_dump(mode="json")
        for key in ("token", "bootstrap_secret", "agent_token"):
            body[key] = getattr(self, key).get_secret_value()
        body["grant"] = self.grant.wire() if self.grant else None
        return json.dumps(body).encode()


def checked(response: httpx.Response) -> dict[str, object]:
    if not response.is_success:
        try:
            body = response.json()
            code, message = (
                body.get("code", "ENROLLMENT_FAILED"),
                body.get("message", "Enrollment failed"),
            )
        except (ValueError, AttributeError):
            code, message = "ENROLLMENT_FAILED", "Scheduler rejected enrollment"
        raise PlatformError(str(code), str(message))
    body = response.json()
    if not isinstance(body, dict):
        raise PlatformError("ENROLLMENT_PROTOCOL", "Scheduler returned invalid enrollment metadata")
    return body


class MacEnrollment:
    def __init__(self, root: Path, http: httpx.Client, tailscale: TailscaleClient) -> None:
        self.root, self.http, self.tailscale = root, http, tailscale
        self.receipt_path = root / "enrollment.json"

    def _prepare(self, scheduler: str, token: str, config: ClusterConfig) -> SetupReceipt:
        origin = https_origin(scheduler)
        fingerprint = hashlib.sha256(config.model_dump_json().encode()).hexdigest()
        self.root.mkdir(parents=True, mode=0o700, exist_ok=True)
        self.root.chmod(0o700)
        if self.receipt_path.exists():
            info = self.receipt_path.stat()
            if info.st_mode & 0o077 or info.st_uid != os.geteuid():
                raise PermissionError("Setup receipt must be owned by this user and mode 0600")
            receipt = SetupReceipt.model_validate_json(self.receipt_path.read_text())
            if (
                receipt.scheduler != origin
                or receipt.inventory_digest != fingerprint
                or not secrets.compare_digest(receipt.token.get_secret_value(), token)
            ):
                raise PlatformError(
                    "ENROLLMENT_PENDING",
                    "Retry the existing enrollment with its original inventory",
                )
            return receipt
        if (self.root / "credentials.json").exists():
            raise PlatformError("CLUSTER_REGISTERED", "Cluster is enrolled; use setup reconfigure")
        if config.cluster_id is not None:
            raise PlatformError(
                "CLUSTER_ID_CONFLICT", "New inventory must not specify a cluster UUID"
            )
        receipt = SetupReceipt(
            scheduler=origin,
            token=SecretStr(token),
            inventory_digest=fingerprint,
            bootstrap_secret=SecretStr(secrets.token_urlsafe(48)),
            agent_token=SecretStr(secrets.token_urlsafe(48)),
        )
        atomic_write(self.receipt_path, receipt.protected_bytes())
        return receipt

    def enroll(self, scheduler: str, token: str, config: ClusterConfig) -> ClusterConfig:
        self.root.mkdir(parents=True, mode=0o700, exist_ok=True)
        self.root.chmod(0o700)
        lock = ExclusiveLock(self.root / "enrollment.lock")
        lock.acquire()
        try:
            return self._enroll(scheduler, token, config)
        finally:
            lock.release()

    def _enroll(self, scheduler: str, token: str, config: ClusterConfig) -> ClusterConfig:
        receipt = self._prepare(scheduler, token, config)
        write_config(self.root, config)
        headers = {"Authorization": "Bearer " + receipt.token.get_secret_value()}
        if receipt.grant is None:
            body = checked(
                self.http.post(
                    receipt.scheduler + "/api/enrollment/claim",
                    headers=headers,
                    json={"bootstrap_secret": receipt.bootstrap_secret.get_secret_value()},
                )
            )
            receipt.grant = EnrollmentGrant.model_validate(body)
            atomic_write(self.receipt_path, receipt.protected_bytes())
        if receipt.node_key is None:
            receipt.node_key = self.tailscale.join(receipt.grant, self.root)
            atomic_write(self.receipt_path, receipt.protected_bytes())
        body = checked(
            self.http.post(
                receipt.scheduler + "/api/enrollment/register",
                headers=headers,
                json={
                    "bootstrap_secret": receipt.bootstrap_secret.get_secret_value(),
                    "config": config.model_dump(mode="json"),
                    "node_key": receipt.node_key,
                    "agent_token": receipt.agent_token.get_secret_value(),
                },
            )
        )
        registered = ClusterConfig.model_validate(body)
        if registered.cluster_id != receipt.grant.cluster_id:
            raise PlatformError(
                "CLUSTER_ID_CONFLICT", "Registration returned a different cluster UUID"
            )
        credentials = AgentCredentials(
            scheduler_url=receipt.grant.agent_origin,
            agent_token=receipt.agent_token,
            enrollment_url=receipt.scheduler,
        )
        atomic_write(self.root / "credentials.json", credentials.protected_bytes())
        write_config(self.root, registered)
        self.receipt_path.unlink()
        fsync_directory(self.root)
        return registered


def unregister(
    root: Path, snapshot: ClusterSnapshot, *, client: httpx.Client | None = None
) -> None:
    if client is None:
        with httpx.Client(timeout=30, follow_redirects=False) as http:
            unregister(root, snapshot, client=http)
        return
    credentials = load_credentials(root / "credentials.json")
    if credentials is None:
        raise PlatformError(
            "CREDENTIALS_MISSING", "Restore agent credentials before retrying cluster destroy"
        )
    if snapshot.cluster.cluster_id is None:
        raise PlatformError("CLUSTER_ID_CONFLICT", "Enrolled cluster has no UUID")
    # The narrow public endpoint remains reachable after a lost response and node revocation.
    endpoint = (
        https_origin(credentials.enrollment_url)
        + f"/api/enrollment/clusters/{snapshot.cluster.cluster_id}/unregister"
        if credentials.enrollment_url
        else https_origin(credentials.scheduler_url)
        + f"/api/agents/{snapshot.cluster.cluster_id}/unregister"
    )
    checked(
        client.post(
            endpoint,
            headers={"Authorization": "Bearer " + credentials.agent_token.get_secret_value()},
            json=snapshot.model_dump(mode="json"),
        )
    )


def set_unregister_origin(root: Path, scheduler: str) -> None:
    """Give older enrolled credentials an explicit public origin for revocation-safe retry."""
    origin = https_origin(scheduler)
    credentials = load_credentials(root / "credentials.json")
    if credentials is not None:
        credentials.enrollment_url = origin
        atomic_write(root / "credentials.json", credentials.protected_bytes())
