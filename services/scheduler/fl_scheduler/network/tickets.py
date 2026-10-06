"""Short PostgreSQL transactions for the enrollment saga. No network calls hold locks."""

import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta
from uuid import UUID, uuid4

from cryptography.fernet import Fernet, InvalidToken
from fl_common.errors import PlatformError
from fl_common.models.base import utcnow
from fl_common.models.enrollment import EnrollmentGrant, EnrollmentIssued
from fl_common.network import https_origin
from pydantic import SecretStr
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from ..auth.sessions import digest
from ..db.core import Database
from .headscale import NetworkKey
from .models import Enrollment, NetworkRevocation


def queue_revocation(
    session: Session,
    kind: str,
    object_id: str,
    *,
    cluster_id: UUID | None = None,
    finish_after: datetime | None = None,
) -> None:
    session.execute(
        insert(NetworkRevocation)
        .values(
            revocation_id=uuid4(),
            kind=kind,
            object_id=object_id,
            cluster_id=cluster_id,
            state="PENDING",
            attempts=0,
            next_attempt_at=utcnow(),
            finish_after=finish_after,
        )
        .on_conflict_do_nothing(constraint="network_revocation_identity")
    )


def checked_ticket(session: Session, token: str, bootstrap: str | None = None) -> Enrollment:
    row = session.scalar(
        select(Enrollment).where(Enrollment.token_hash == digest(token)).with_for_update()
    )
    if (
        row is None
        or row.state in {"REVOKED", "EXPIRED"}
        or (row.state != "REGISTERED" and row.expires_at <= utcnow())
    ):
        raise PlatformError("ENROLLMENT_INVALID", "Enrollment is unavailable or expired")
    if (
        bootstrap is not None
        and row.bootstrap_hash
        and not secrets.compare_digest(row.bootstrap_hash, digest(bootstrap))
    ):
        raise PlatformError("ENROLLMENT_CLAIMED", "Enrollment belongs to another setup attempt")
    return row


@dataclass(frozen=True)
class ClaimLease:
    enrollment_id: UUID
    cluster_id: UUID
    lease_id: UUID | None
    expires_at: datetime
    grant: EnrollmentGrant | None = None


class Tickets:
    def __init__(
        self, db: Database, encryption_key: SecretStr, login_url: str, agent_origin: str
    ) -> None:
        self.db, self.login_url = db, https_origin(login_url)
        self.agent_origin = https_origin(agent_origin)
        self.cipher = Fernet(encryption_key.get_secret_value().encode())

    def issue(self, subject: str, lifetime: int = 1800) -> EnrollmentIssued:
        if not 60 <= lifetime <= 3600:
            raise ValueError("Enrollment lifetime must be between one minute and one hour")
        secret = secrets.token_urlsafe(48)
        enrollment_id, cluster_id = uuid4(), uuid4()
        expires = utcnow() + timedelta(seconds=lifetime)
        with self.db.transaction() as session:
            session.add(
                Enrollment(
                    enrollment_id=enrollment_id,
                    cluster_id=cluster_id,
                    token_hash=digest(secret),
                    created_by=subject,
                    expires_at=expires,
                )
            )
        return EnrollmentIssued(
            enrollment_id=enrollment_id,
            cluster_id=cluster_id,
            token=SecretStr(secret),
            expires_at=expires,
        )

    def claim(self, token: str, bootstrap: str) -> ClaimLease:
        with self.db.transaction() as session:
            row = checked_ticket(session, token, bootstrap)
            if row.state == "REGISTERED":
                raise PlatformError("ENROLLMENT_REGISTERED", "Cluster is already registered")
            if row.key_ciphertext:
                if row.key_expires_at is None or row.key_expires_at <= utcnow():
                    raise PlatformError("ENROLLMENT_KEY_EXPIRED", "Network join key expired")
                try:
                    key = self.cipher.decrypt(row.key_ciphertext.encode()).decode()
                except InvalidToken as error:
                    raise PlatformError(
                        "ENROLLMENT_STATE", "Enrollment receipt cannot be read"
                    ) from error
                return ClaimLease(
                    row.enrollment_id,
                    row.cluster_id,
                    None,
                    row.key_expires_at,
                    EnrollmentGrant(
                        cluster_id=row.cluster_id,
                        headscale_url=self.login_url,
                        agent_origin=self.agent_origin,
                        auth_key=SecretStr(key),
                        expires_at=row.key_expires_at,
                    ),
                )
            if row.lease_expires_at and row.lease_expires_at > utcnow():
                raise PlatformError(
                    "ENROLLMENT_PENDING", "Network key issuance is already in progress"
                )
            row.bootstrap_hash = digest(bootstrap)
            row.state, row.lease_id = "CLAIMING", uuid4()
            row.lease_expires_at = utcnow() + timedelta(seconds=45)
            expires = min(row.expires_at, utcnow() + timedelta(minutes=10))
            return ClaimLease(row.enrollment_id, row.cluster_id, row.lease_id, expires)

    def save_key(self, lease: ClaimLease, key: NetworkKey) -> EnrollmentGrant:
        with self.db.transaction() as session:
            row = session.scalar(
                select(Enrollment)
                .where(Enrollment.enrollment_id == lease.enrollment_id)
                .with_for_update()
            )
            if (
                row is None
                or row.state != "CLAIMING"
                or row.lease_id != lease.lease_id
                or (row.expires_at <= utcnow())
            ):
                queue_revocation(
                    session, "KEY", key.key_id, finish_after=key.expires_at + timedelta(minutes=1)
                )
                unavailable = True
            else:
                row.state = "CLAIMED"
                row.key_id, row.key_expires_at = key.key_id, key.expires_at
                row.key_ciphertext = self.cipher.encrypt(
                    key.key.get_secret_value().encode()
                ).decode()
                row.lease_id, row.lease_expires_at = None, None
                unavailable = False
        if unavailable:
            raise PlatformError("ENROLLMENT_INVALID", "Enrollment was revoked during key issuance")
        return EnrollmentGrant(
            cluster_id=lease.cluster_id,
            headscale_url=self.login_url,
            agent_origin=self.agent_origin,
            auth_key=key.key,
            expires_at=key.expires_at,
        )

    def release_lease(self, lease: ClaimLease) -> None:
        with self.db.transaction() as session:
            row = session.scalar(
                select(Enrollment)
                .where(Enrollment.enrollment_id == lease.enrollment_id)
                .with_for_update()
            )
            if row and row.lease_id == lease.lease_id:
                row.lease_id, row.lease_expires_at = None, None

    def revoke(self, enrollment_id: UUID) -> None:
        with self.db.transaction() as session:
            row = session.get(Enrollment, enrollment_id, with_for_update=True)
            if row is None:
                raise PlatformError("ENROLLMENT_INVALID", "Unknown enrollment")
            if row.state == "REGISTERED":
                raise PlatformError(
                    "ENROLLMENT_REGISTERED", "Destroy the registered cluster instead"
                )
            self._retire(session, row, "REVOKED")

    @staticmethod
    def _retire(session: Session, row: Enrollment, state: str) -> None:
        row.state, row.key_ciphertext = state, None
        if row.key_id:
            finish = (row.key_expires_at or row.expires_at) + timedelta(minutes=1)
            queue_revocation(session, "KEY", row.key_id, finish_after=finish)

    def expire(self) -> None:
        with self.db.transaction() as session:
            rows = session.scalars(
                select(Enrollment)
                .where(
                    Enrollment.state.in_(["ISSUED", "CLAIMING", "CLAIMED"]),
                    Enrollment.expires_at <= utcnow(),
                )
                .with_for_update()
            )
            for row in rows:
                self._retire(session, row, "EXPIRED")
