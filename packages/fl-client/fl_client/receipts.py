"""Save the identity, staging secret and nonce before sending any submission request."""

import hashlib
import json
import secrets
from pathlib import Path
from uuid import uuid4

from fl_common.errors import PlatformError
from fl_common.files import atomic_write, sha256_file
from fl_common.locks import ExclusiveLock
from fl_common.models import ArtifactRef, JobConfig, JobSpec
from fl_common.models.base import Schema
from fl_common.models.scheduler import Principal
from fl_common.models.submission import SubmissionRequest
from fl_common.network import https_origin
from fl_common.private_files import check_private, read_private
from fl_common.ssh import create_identity, identity_public_key
from pydantic import SecretStr, field_validator, model_validator


class Receipt(Schema):
    scheduler: str
    request: SubmissionRequest
    principal: Principal | None = None
    staging_token: SecretStr | None = None
    spec: JobSpec | None = None
    delivered: bool = False

    @field_validator("scheduler")
    @classmethod
    def origin(cls, value: str) -> str:
        return https_origin(value)

    @model_validator(mode="after")
    def proof(self) -> "Receipt":
        if self.request.public_key is not None:
            if (
                self.staging_token is None
                or hashlib.sha256(self.staging_token.get_secret_value().encode()).hexdigest()
                != self.request.token_hash
            ):
                raise ValueError("Receipt staging proof differs from submission metadata")
        return self

    def protected_bytes(self) -> bytes:
        body = self.model_dump(mode="json")
        if self.staging_token:
            body["staging_token"] = self.staging_token.get_secret_value()
        return json.dumps(body).encode()


class ReceiptStore:
    def __init__(self, path: Path) -> None:
        self.path = path.absolute()
        self.identity = self.path.with_name(self.path.name + ".key")

    def lock(self) -> ExclusiveLock:
        self.path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
        check_private(self.path.parent.lstat(), directory=True)
        return ExclusiveLock(self.path.with_name(self.path.name + ".lock"))

    def load(self) -> Receipt:
        receipt = Receipt.model_validate_json(read_private(self.path))
        if (
            receipt.request.public_key is not None
            and identity_public_key(self.identity) != receipt.request.public_key
        ):
            raise PlatformError(
                "TRANSFER_IDENTITY", "Receipt identity no longer matches its request"
            )
        return receipt

    def save(self, receipt: Receipt) -> None:
        atomic_write(self.path, receipt.protected_bytes())

    def prepare(self, scheduler: str, config: JobConfig) -> Receipt:
        if self.path.exists() or self.identity.exists():
            raise PlatformError("RECEIPT_EXISTS", "Use --resume for an existing receipt")
        config = config.model_copy(deep=True)
        refs: dict[str, ArtifactRef | None] = {"binary": None, "bitstream": None}
        for kind in ("binary", "bitstream"):
            if filename := getattr(config, kind):
                source = Path(filename).resolve()
                setattr(config, kind, str(source))
                digest, size = sha256_file(source)
                refs[kind] = ArtifactRef.model_validate(
                    {
                        "kind": kind,
                        "sha256": digest,
                        "size_bytes": size,
                        "original_name": source.name,
                    }
                )
        token = SecretStr(secrets.token_urlsafe(32)) if any(refs.values()) else None
        key = create_identity(self.identity) if token else None
        request = SubmissionRequest(
            request_id=uuid4(),
            config=config,
            binary=refs["binary"],
            bitstream=refs["bitstream"],
            public_key=key,
            token_hash=hashlib.sha256(token.get_secret_value().encode()).hexdigest()
            if token
            else None,
        )
        receipt = Receipt(scheduler=scheduler, request=request, staging_token=token)
        self.save(receipt)
        return receipt
