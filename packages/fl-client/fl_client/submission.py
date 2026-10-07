"""Ordinary-user submission: public HTTPS metadata and Rclone, with durable local retry proof."""

import asyncio
import time
from collections.abc import Callable
from pathlib import Path

from fl_common.errors import PlatformError
from fl_common.files import sha256_file
from fl_common.models import ArtifactRef, JobConfig, JobSpec
from fl_common.models.scheduler import Principal
from fl_common.models.submission import SubmissionResponse, UploadTicket
from fl_common.rclone import Rclone
from pydantic import TypeAdapter

from .api import RemoteClient, checked
from .receipts import Receipt, ReceiptStore


class Submission:
    def __init__(
        self,
        client: RemoteClient,
        *,
        rclone: Rclone | None = None,
        display: Callable[[str], None] = print,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.client, self.rclone, self.display, self.sleep = client, rclone, display, sleep

    def run(self, store: ReceiptStore, config: JobConfig | None = None) -> JobSpec:
        with store.lock():
            origin = self.client.origin()
            receipt = store.prepare(origin, config) if config is not None else store.load()
            if receipt.scheduler != origin:
                raise PlatformError(
                    "SUBMISSION_ORIGIN", "Resume using this receipt's scheduler login"
                )
            principal = Principal.model_validate(self.client.request("GET", "/api/auth/me"))
            if (
                receipt.principal is not None
                and (receipt.principal.email, receipt.principal.subject)
                != (principal.email, principal.subject)
            ) or (receipt.spec is not None and receipt.spec.owner != principal.email):
                raise PlatformError(
                    "SUBMISSION_OWNER", "Resume using the original user's scheduler login"
                )
            receipt.principal = principal
            store.save(receipt)
            self.display("Receipt: " + str(store.path))
            response = SubmissionResponse.model_validate(
                self.client.request(
                    "POST", "/api/submissions", json=receipt.request.model_dump(mode="json")
                )
            )
            if receipt.spec is not None and receipt.spec != response.spec:
                raise PlatformError(
                    "SUBMISSION_ID_CONFLICT", "Scheduler returned a different job for this receipt"
                )
            receipt.spec = response.spec
            store.save(receipt)
            self.display("Job: " + str(response.spec.job_id))
            if receipt.delivered or response.state.terminal:
                return response.spec
            self.deliver(store, receipt)
            return response.spec

    def deliver(self, store: ReceiptStore, receipt: Receipt) -> None:
        assert receipt.spec is not None
        job_id = receipt.spec.job_id
        waiting = False
        while True:
            ticket = UploadTicket.model_validate(
                self.client.request("POST", f"/api/jobs/{job_id}/upload")
            )
            if ticket.job_id != job_id:
                raise PlatformError("TRANSFER_SCOPE", "Upload ticket belongs to another job")
            if ticket.state in {"DELIVERING", "COMPLETE"}:
                receipt.delivered = True
                store.save(receipt)
                return
            if ticket.state == "WAITING":
                if not waiting:
                    self.display("Waiting for a board reservation")
                    waiting = True
                self.sleep(2)
                continue
            grant = ticket.grant
            assert (
                grant is not None
                and ticket.endpoint is not None
                and ticket.verify_origin is not None
            )
            refs = [ref for ref in (receipt.request.binary, receipt.request.bitstream) if ref]
            if (
                grant.public_key != receipt.request.public_key
                or grant.token_hash != receipt.request.token_hash
                or grant.files != refs
            ):
                raise PlatformError("TRANSFER_SCOPE", "Upload scope differs from this receipt")
            if not self.verify(ticket, receipt, incomplete=True):
                for ref in refs:
                    source = Path(str(getattr(receipt.request.config, ref.kind)))
                    if sha256_file(source) != (ref.sha256, ref.size_bytes):
                        raise PlatformError(
                            "ARTIFACT_CHANGED",
                            "Input changed after receipt creation; submit a new request",
                        )
                transport = self.rclone or Rclone()
                for ref in refs:
                    source = Path(str(getattr(receipt.request.config, ref.kind)))
                    self.display("Uploading " + ref.kind)
                    asyncio.run(
                        transport.copy(source, ticket.endpoint, grant, ref.kind, store.identity)
                    )
                self.verify(ticket, receipt)
            self.client.request(
                "POST", f"/api/jobs/{job_id}/delivery", json={"upload_id": str(grant.transfer_id)}
            )
            receipt.delivered = True
            store.save(receipt)
            self.display("Verified collateral accepted for Mac delivery")
            return

    def verify(self, ticket: UploadTicket, receipt: Receipt, *, incomplete: bool = False) -> bool:
        assert (
            ticket.grant is not None
            and ticket.verify_origin is not None
            and receipt.staging_token is not None
        )
        response = self.client.http.post(
            ticket.verify_origin + f"/api/uploads/{ticket.grant.transfer_id}/verify",
            headers={"Authorization": "Bearer " + receipt.staging_token.get_secret_value()},
            timeout=600,
        )
        try:
            refs = TypeAdapter(list[ArtifactRef]).validate_python(checked(response))
        except PlatformError as error:
            if incomplete and error.code == "TRANSFER_INCOMPLETE":
                return False
            raise
        if refs != ticket.grant.files:
            raise PlatformError(
                "TRANSFER_SCOPE", "Gateway attestation differs from the upload manifest"
            )
        return True
