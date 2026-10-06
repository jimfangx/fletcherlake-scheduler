"""Atomic OpenSSH key projection; every accepted identity runs one fixed transfer guard."""

import shlex
import sqlite3
import sys
from pathlib import Path

from fl_common.files import atomic_write
from fl_common.models.base import utcnow
from fl_common.models.transfer import TransferGrant


def write_keys(root: Path, bbcp: Path, first: int, last: int, rows: list[sqlite3.Row]) -> None:
    lines = []
    for row in rows:
        grant = TransferGrant.model_validate_json(row["grant_json"])
        if row["state"] == "REVOKED" or grant.expires_at <= utcnow():
            continue
        if grant.direction == "upload" and row["state"] != "OPEN":
            continue
        command = shlex.join(
            [
                sys.executable,
                "-I",
                "-m",
                "fl_gateway.ssh",
                "--root",
                str(root),
                "--bbcp",
                str(bbcp),
                "--transfer",
                str(grant.transfer_id),
                "--data-port-first",
                str(first),
                "--data-port-last",
                str(last),
            ]
        )
        escaped = command.replace("\\", "\\\\").replace('"', '\\"')
        options = f'restrict,command="{escaped}"'
        if grant.source_networks:
            options += ',from="' + ",".join(grant.source_networks) + '"'
        lines.append(f"{options} {grant.public_key}\n")
    atomic_write(root / "authorized_keys", "".join(lines).encode())
