"""Small transfer-scope factory shared by gateway protocol and payload tests."""

import hashlib
from datetime import timedelta
from uuid import uuid4

from fl_common.models import ArtifactRef
from fl_common.models.base import utcnow
from fl_common.models.transfer import TransferGrant
from fl_common.ssh import create_identity

TOKEN = "protected test receipt with at least 32 characters"
TOKEN_HASH = hashlib.sha256(TOKEN.encode()).hexdigest()


def grant(tmp_path, data, *, source=None):
    identity = tmp_path / f"identity-{uuid4()}"
    scope = TransferGrant(
        transfer_id=uuid4(),
        job_id=source.job_id if source else uuid4(),
        direction="download" if source else "upload",
        public_key=create_identity(identity),
        token_hash=TOKEN_HASH,
        files=source.files
        if source
        else [
            ArtifactRef(
                kind="binary", sha256=hashlib.sha256(data).hexdigest(), size_bytes=len(data)
            )
        ],
        expires_at=utcnow() + timedelta(seconds=45),
        retains_until=source.retains_until if source else utcnow() + timedelta(minutes=10),
        source_id=source.transfer_id if source else None,
        source_networks=["127.0.0.1/32"] if source else [],
    )
    return scope, identity
