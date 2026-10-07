"""A manifest-only SFTP v3 filesystem over OpenSSH's authenticated stdio channel.

No supplied path is joined to a filesystem root. Paths select only fixed artifact kinds
in one grant; mutation, links, shell commands and arbitrary directory traversal are denied.
Uploads use bounded random-access partial files and publish atomically after SHA verification.
"""

import os
import secrets
import stat
import struct
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, Self

from fl_common.errors import PlatformError
from fl_common.files import sha256_file
from fl_common.locks import ExclusiveLock
from fl_common.models.artifact import ArtifactKind, ArtifactRef
from fl_common.models.transfer import TransferGrant

from .store import GatewayStore

MAX_PACKET = 1024 * 1024
MAX_IO = 256 * 1024
MAX_HANDLES = 16
# SFTP v3 packet types and status codes (draft-ietf-secsh-filexfer-02).
INIT, VERSION, OPEN, CLOSE, READ, WRITE = 1, 2, 3, 4, 5, 6
LSTAT, FSTAT, SETSTAT, FSETSTAT, OPENDIR, READDIR = 7, 8, 9, 10, 11, 12
MKDIR, REALPATH, STAT = 14, 16, 17
STATUS, HANDLE, DATA, NAME, ATTRS = 101, 102, 103, 104, 105
OK, EOF, NO_SUCH_FILE, DENIED, FAILURE, BAD_MESSAGE, UNSUPPORTED = 0, 1, 2, 3, 4, 5, 8


def u32(value: int) -> bytes:
    return struct.pack(">I", value)


def string(value: bytes) -> bytes:
    return u32(len(value)) + value


class Packet:
    def __init__(self, data: bytes) -> None:
        self.data, self.offset = data, 0

    def take(self, length: int) -> bytes:
        if length < 0 or self.offset + length > len(self.data):
            raise ValueError("Truncated SFTP packet")
        result = self.data[self.offset : self.offset + length]
        self.offset += length
        return result

    def number(self) -> int:
        return int(struct.unpack(">I", self.take(4))[0])

    def offset64(self) -> int:
        return int(struct.unpack(">Q", self.take(8))[0])

    def text(self) -> bytes:
        return self.take(self.number())

    def end(self) -> None:
        if self.offset != len(self.data):
            raise ValueError("Unexpected SFTP packet fields")


@dataclass
class File:
    kind: ArtifactKind
    ref: ArtifactRef
    stream: BinaryIO
    lock: ExclusiveLock
    partial: Path | None = None

    def discard(self) -> None:
        try:
            self.stream.close()
            if self.partial is not None:
                self.partial.unlink(missing_ok=True)
        finally:
            self.lock.release()


class SFTP:
    def __init__(self, store: GatewayStore, grant: TransferGrant) -> None:
        self.store, self.grant = store, grant
        self.prefix = f"/transfer/{grant.transfer_id}"
        self.refs = {ref.kind: ref for ref in grant.files}
        self.files: dict[bytes, File] = {}
        self.directories: dict[bytes, str | None] = {}

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *args: object) -> None:
        for handle in list(self.files):
            self.files.pop(handle).discard()

    def active(self) -> None:
        grant, state = self.store.lookup(self.grant.transfer_id)
        if grant.direction == "upload" and state != "OPEN":
            raise PlatformError("TRANSFER_SCOPE", "Uploads are immutable after verification")
        if grant.direction == "download":
            self.store.source(grant)

    def path(self, raw: bytes) -> str:
        name = raw.decode("utf-8")
        if name in ("", "."):
            return "/"
        # Canonical paths only: never normalize away traversal or encoded aliases.
        if "\0" in name or "\\" in name or any(p in (".", "..") for p in name.split("/")):
            raise PlatformError("TRANSFER_SCOPE", "Invalid virtual path")
        name = "/" + name.strip("/")
        allowed = {"/", "/transfer", self.prefix} | {f"{self.prefix}/{kind}" for kind in self.refs}
        if name not in allowed:
            raise PlatformError("TRANSFER_SCOPE", "Path is outside this grant")
        return name

    def kind(self, name: str) -> ArtifactKind:
        for kind in self.refs:
            if name == f"{self.prefix}/{kind}":
                return kind
        raise PlatformError("TRANSFER_SCOPE", "Expected an exact manifest file")

    def attributes(self, name: str) -> bytes:
        if name in ("/", "/transfer", self.prefix):
            return u32(4) + u32(stat.S_IFDIR | 0o700)
        kind = self.kind(name)
        source = self.store.source(self.grant) if self.grant.direction == "download" else self.grant
        target = self.store.path(source, kind)
        info = target.lstat()
        if not stat.S_ISREG(info.st_mode):
            raise PlatformError("TRANSFER_SCOPE", "Expected a regular manifest file")
        return u32(13) + struct.pack(
            ">QIII", info.st_size, stat.S_IFREG | 0o600, int(info.st_atime), int(info.st_mtime)
        )

    def open(self, name: str, flags: int) -> bytes:
        if len(self.files) + len(self.directories) >= MAX_HANDLES:
            raise PlatformError("TRANSFER_SCOPE", "Too many open handles")
        kind = self.kind(name)
        upload = self.grant.direction == "upload"
        # WRITE|CREAT|TRUNC for uploads; READ alone for downloads. No read/write or append.
        if flags != (26 if upload else 1):
            raise PlatformError("TRANSFER_SCOPE", "Access mode differs from the grant")
        lock = self.store.lock(self.grant.transfer_id)
        lock.acquire()
        partial = None
        stream = None
        try:
            self.active()
            if upload:
                target = self.store.path(self.grant, kind)
                target.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
                descriptor, filename = tempfile.mkstemp(prefix=".partial-", dir=target.parent)
                partial = Path(filename)
                stream = os.fdopen(descriptor, "w+b", buffering=0)
            else:
                target = self.store.path(self.store.source(self.grant), kind)
                descriptor = os.open(target, os.O_RDONLY | os.O_NOFOLLOW)
                stream = os.fdopen(descriptor, "rb", buffering=0)
                if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                    raise PlatformError("TRANSFER_SCOPE", "Expected a regular manifest file")
                ref = self.refs[kind]
                if sha256_file(target) != (ref.sha256, ref.size_bytes):
                    raise PlatformError("ARTIFACT_INTEGRITY", "Verified gateway content changed")
            handle = secrets.token_bytes(16)
            self.files[handle] = File(kind, self.refs[kind], stream, lock, partial)
            return handle
        except BaseException:
            if stream is not None:
                stream.close()
            if partial is not None:
                partial.unlink(missing_ok=True)
            lock.release()
            raise

    def close(self, handle: bytes) -> None:
        if handle in self.directories:
            del self.directories[handle]
            return
        item = self.files.pop(handle)
        try:
            self.active()
            if item.partial is not None:
                item.stream.flush()
                os.fsync(item.stream.fileno())
                if sha256_file(item.partial) != (item.ref.sha256, item.ref.size_bytes):
                    raise PlatformError("ARTIFACT_INTEGRITY", "Received bytes differ from manifest")
                self.store.received(self.grant, item.ref, item.partial)
        finally:
            item.discard()

    @staticmethod
    def status(code: int) -> tuple[int, bytes]:
        return STATUS, u32(code) + string(
            b"Operation rejected" if code not in (OK, EOF) else b""
        ) + string(b"")

    def dispatch(self, opcode: int, packet: Packet) -> tuple[int, bytes]:
        self.active()
        if opcode == REALPATH:
            name = self.path(packet.text())
            packet.end()
            return NAME, u32(1) + string(name.encode()) + string(name.encode()) + u32(0)
        if opcode in (STAT, LSTAT):
            name = self.path(packet.text())
            packet.end()
            return ATTRS, self.attributes(name)
        if opcode == OPEN:
            name, flags = self.path(packet.text()), packet.number()
            # Ignore metadata for creation, but parse it strictly; never apply permissions/size.
            attrs = packet.number()
            if attrs & ~15:
                raise ValueError("Unsupported creation attributes")
            for bit, length in ((1, 8), (2, 8), (4, 4), (8, 8)):
                if attrs & bit:
                    packet.take(length)
            packet.end()
            return HANDLE, string(self.open(name, flags))
        if opcode == CLOSE:
            handle = packet.text()
            packet.end()
            self.close(handle)
            return self.status(OK)
        if opcode == FSTAT:
            item = self.files[packet.text()]
            packet.end()
            size = os.fstat(item.stream.fileno()).st_size
            return ATTRS, u32(5) + struct.pack(">QI", size, stat.S_IFREG | 0o600)
        if opcode in (READ, WRITE):
            item = self.files[packet.text()]
            offset = packet.offset64()
            if opcode == READ:
                length = packet.number()
                packet.end()
                if item.partial is not None:
                    raise PlatformError("TRANSFER_SCOPE", "An upload grant cannot read files")
                if length > MAX_IO:
                    raise ValueError("Read exceeds packet bound")
                data = os.pread(item.stream.fileno(), min(length, item.ref.size_bytes), offset)
                return (DATA, string(data)) if data else self.status(EOF)
            data = packet.text()
            packet.end()
            if (
                item.partial is None
                or len(data) > MAX_IO
                or offset + len(data) > item.ref.size_bytes
            ):
                raise PlatformError("ARTIFACT_INTEGRITY", "Write exceeds declared scope/size")
            # pwrite supports rclone's pipelined, potentially out-of-order blocks.
            done = 0
            while done < len(data):
                written = os.pwrite(item.stream.fileno(), data[done:], offset + done)
                if written == 0:
                    raise OSError("Short file write")
                done += written
            return self.status(OK)
        if opcode == OPENDIR:
            name = self.path(packet.text())
            packet.end()
            if (
                name not in ("/", "/transfer", self.prefix)
                or len(self.files) + len(self.directories) >= MAX_HANDLES
            ):
                raise PlatformError("TRANSFER_SCOPE", "Directory is outside this grant")
            handle = secrets.token_bytes(16)
            self.directories[handle] = name
            return HANDLE, string(handle)
        if opcode == READDIR:
            handle = packet.text()
            packet.end()
            directory = self.directories[handle]
            if directory is None:
                return self.status(EOF)
            self.directories[handle] = None
            if directory == "/":
                names = ["transfer"]
            elif directory == "/transfer":
                names = [str(self.grant.transfer_id)]
            else:
                names = []
                for kind in self.refs:
                    try:
                        self.attributes(f"{self.prefix}/{kind}")
                        names.append(kind)
                    except FileNotFoundError:
                        pass
            entries = b""
            for child in names:
                path = ("" if directory == "/" else directory) + "/" + child
                entries += string(child.encode()) + string(child.encode()) + self.attributes(path)
            return (NAME, u32(len(names)) + entries) if names else self.status(EOF)
        if opcode == MKDIR:
            # Parent directories are virtual and already exist; no supplied directory is created.
            name = self.path(packet.text())
            if name not in ("/", "/transfer", self.prefix):
                raise PlatformError("TRANSFER_SCOPE", "Cannot create directories")
            return self.status(OK)
        # Includes SETSTAT/FSETSTAT, REMOVE/RMDIR, RENAME, READLINK/SYMLINK and extensions.
        return self.status(UNSUPPORTED)

    def handle(self, opcode: int, data: bytes) -> tuple[int, bytes]:
        try:
            return self.dispatch(opcode, Packet(data))
        except FileNotFoundError:
            return self.status(NO_SUCH_FILE)
        except (PlatformError, KeyError, PermissionError):
            return self.status(DENIED)
        except (ValueError, UnicodeError, struct.error, OverflowError):
            return self.status(BAD_MESSAGE)
        except OSError:
            return self.status(FAILURE)

    def serve(self, input_file: BinaryIO, output: BinaryIO) -> None:
        initialized = False
        while header := input_file.read(4):
            if len(header) != 4:
                raise ValueError("Truncated SFTP frame")
            length = struct.unpack(">I", header)[0]
            if not 5 <= length <= MAX_PACKET:
                raise ValueError("Invalid SFTP frame length")
            frame = input_file.read(length)
            if len(frame) != length:
                raise ValueError("Truncated SFTP frame")
            opcode, packet = frame[0], Packet(frame[1:])
            if not initialized:
                if opcode != INIT or packet.number() != 3:
                    raise ValueError("Expected SFTP v3 initialization")
                packet.end()
                reply = bytes([VERSION]) + u32(3)
                initialized = True
            else:
                request_id = packet.take(4)
                response, payload = self.handle(opcode, packet.data[packet.offset :])
                reply = bytes([response]) + request_id + payload
            output.write(u32(len(reply)) + reply)
            output.flush()
