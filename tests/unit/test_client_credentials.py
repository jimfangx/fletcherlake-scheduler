"""Credentials must stay private and cannot silently redirect the authenticated client."""

from datetime import timedelta

import pytest
from fl_client.credentials import Credentials, CredentialStore, https_origin
from fl_common.models.base import utcnow
from pydantic import SecretStr


def tokens():
    return Credentials(
        scheduler="https://scheduler.test",
        access_token=SecretStr("private-access"),
        refresh_token=SecretStr("private-refresh"),
        access_expires_at=utcnow() + timedelta(minutes=15),
        refresh_expires_at=utcnow() + timedelta(days=7),
    )


def test_credentials_protected_roundtrip_masked_repr_and_bad_permissions(tmp_path):
    store = CredentialStore(tmp_path / "private" / "client.json")
    with store.locked():
        store.save(tokens())
        credentials = store.load()
    assert credentials.access_token.get_secret_value() == "private-access"
    assert "private-access" not in repr(credentials)
    assert "private-refresh" not in credentials.model_dump_json()
    store.path.chmod(0o644)
    with pytest.raises(PermissionError):
        store.load()


def test_credentials_refuse_symlink_and_public_parent(tmp_path):
    store = CredentialStore(tmp_path / "private" / "client.json")
    with store.locked():
        target = store.path.with_name("other.json")
        target.write_bytes(tokens().protected_bytes())
        target.chmod(0o600)
        store.path.symlink_to(target)
        with pytest.raises(OSError):
            store.load()
    store.path.parent.chmod(0o755)
    with pytest.raises(PermissionError), store.locked():
        pass


@pytest.mark.parametrize(
    "origin",
    [
        "http://scheduler.test",
        "https://user:secret@scheduler.test",
        "https://scheduler.test/api",
        "https://scheduler.test?token=x",
        "https://scheduler.test#fragment",
    ],
)
def test_credentials_require_plain_https_origin(origin):
    with pytest.raises(ValueError):
        https_origin(origin)
