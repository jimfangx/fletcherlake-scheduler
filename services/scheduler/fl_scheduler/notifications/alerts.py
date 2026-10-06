"""Retention warnings are events, separate from delivery providers and hardware state."""

from datetime import timedelta

from fl_common.models import ArtifactRecord
from fl_common.models.base import utcnow
from fl_common.models.events import TERMINAL_STATES
from fl_common.protocol import MessageType
from sqlalchemy import DateTime, cast, exists, select

from ..db.core import Database
from ..db.models import Artifact, Command, Event, Job
from .models import AlertMarker


class Alerts:
    def __init__(self, db: Database) -> None:
        self.db = db

    def sweep(self) -> None:
        now = utcnow()
        with self.db.transaction(placement=True) as session:
            deleting = exists().where(
                Command.job_id == Job.job_id,
                Command.envelope["type"].as_string() == MessageType.ARTIFACT_DELETE,
            )
            expiry_time = cast(
                Artifact.metadata_json["expires_at"].as_string(), DateTime(timezone=True)
            )
            warned = (
                select(AlertMarker.key)
                .join(Event)
                .where(
                    Event.job_id == Artifact.job_id,
                    cast(Event.payload["expires_at"].as_string(), DateTime(timezone=True))
                    == expiry_time,
                )
                .exists()
            )
            rows = session.execute(
                select(Artifact, Job)
                .join(Job)
                .where(
                    Job.state.in_(list(TERMINAL_STATES)),
                    ~deleting,
                    ~warned,
                    Artifact.metadata_json["deleted_at"].as_string().is_(None),
                    expiry_time > now,
                    expiry_time <= now + timedelta(days=1),
                )
                .order_by(Artifact.job_id, Artifact.kind)
                .limit(100)
            )
            for artifact, job in rows:
                record = ArtifactRecord.model_validate(artifact.metadata_json)
                expiry = record.expires_at
                if (
                    record.deleted_at
                    or expiry is None
                    or not now < expiry <= now + timedelta(days=1)
                ):
                    continue
                key = f"artifact-expiring/{job.job_id}/{expiry.isoformat()}"
                if session.get(AlertMarker, key):
                    continue
                event = Event(
                    job_id=job.job_id,
                    type="ARTIFACT_EXPIRING",
                    payload={"expires_at": expiry.isoformat()},
                )
                session.add(event)
                session.flush()
                session.add(AlertMarker(key=key, event_id=event.event_id))
