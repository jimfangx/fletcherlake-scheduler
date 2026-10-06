"""Repair key projections and retry deletion after the durable payload retention deadline."""

import shutil

from fl_common.errors import PlatformError
from fl_common.files import fsync_directory
from fl_common.locks import ExclusiveLock
from fl_common.models.base import utcnow
from fl_common.models.transfer import TransferGrant

from .store import GatewayStore


def sweep(store: GatewayStore) -> None:
    with ExclusiveLock(store.root / "registry.lock"):
        with store.connection() as connection:
            rows = connection.execute("SELECT grant_json FROM transfers").fetchall()
            expired = [
                grant
                for row in rows
                if (grant := TransferGrant.model_validate_json(row["grant_json"])).retains_until
                <= utcnow()
            ]
            for grant in expired:
                connection.execute(
                    "UPDATE transfers SET state='REVOKED' WHERE transfer_id=?",
                    (str(grant.transfer_id),),
                )
        store.repair_authorized_keys()
    for grant in expired:
        try:
            with store.lock(grant.transfer_id):
                if grant.direction == "upload":
                    path = store.path(grant, grant.files[0].kind).parent
                    if path.exists():
                        shutil.rmtree(path)
                        fsync_directory(path.parent)
        except PlatformError as error:
            if error.code != "ALREADY_OWNED":
                raise
            # The committed revocation fences a currently receiving helper. Retry next tick.
            continue
