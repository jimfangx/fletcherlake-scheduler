"""Canonical SSH keys and protected ephemeral identities used only for collateral transfer."""

import os
from pathlib import Path

from cryptography.exceptions import UnsupportedAlgorithm
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

from .files import atomic_write


def public_key(value: str) -> str:
    if len(value) > 200 or "\n" in value or "\r" in value:
        raise ValueError("Expected one Ed25519 SSH public key without a comment")
    try:
        key = serialization.load_ssh_public_key(value.encode())
    except UnsupportedAlgorithm:
        raise ValueError("Transfer keys must be Ed25519") from None
    if not isinstance(key, Ed25519PublicKey):
        raise ValueError("Transfer keys must be Ed25519")
    canonical = key.public_bytes(serialization.Encoding.OpenSSH, serialization.PublicFormat.OpenSSH)
    if value != canonical.decode():
        raise ValueError("Expected canonical SSH key without a comment")
    return value


def create_identity(path: Path) -> str:
    key = Ed25519PrivateKey.generate()
    private = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.OpenSSH,
        serialization.NoEncryption(),
    )
    atomic_write(path, private)
    return (
        key.public_key()
        .public_bytes(serialization.Encoding.OpenSSH, serialization.PublicFormat.OpenSSH)
        .decode()
    )


def identity_public_key(path: Path) -> str:
    """Read a protected identity and return its canonical public counterpart."""
    info = path.stat()
    if path.is_symlink() or info.st_mode & 0o077 or info.st_uid != os.geteuid():
        raise PermissionError("Transfer private key must be owned by this user and mode 0600")
    key = serialization.load_ssh_private_key(path.read_bytes(), password=None)
    if not isinstance(key, Ed25519PrivateKey):
        raise ValueError("Transfer identities must be Ed25519")
    return (
        key.public_key()
        .public_bytes(serialization.Encoding.OpenSSH, serialization.PublicFormat.OpenSSH)
        .decode()
    )
