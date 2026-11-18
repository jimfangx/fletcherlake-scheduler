"""Official mirror failures must not bypass the pinned installer checksum."""

import hashlib
import io
import zipfile
from urllib.error import HTTPError, URLError

import pytest

from tests import download_rclone as installer


@pytest.mark.parametrize(
    ("system", "machine", "target"),
    [
        ("Linux", "x86_64", "linux-amd64"),
        ("Linux", "aarch64", "linux-arm64"),
        ("Darwin", "arm64", "osx-arm64"),
        ("Darwin", "x86_64", "osx-amd64"),
    ],
)
def test_forbidden_primary_uses_verified_native_github_archive(
    tmp_path, monkeypatch, system, machine, target
):
    payload = b"verified native rclone binary"
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr(f"rclone-{installer.VERSION}-{target}/rclone", payload)
    data = buffer.getvalue()
    monkeypatch.setattr(installer.platform, "system", lambda: system)
    monkeypatch.setattr(installer.platform, "machine", lambda: machine)
    monkeypatch.setitem(
        installer.CHECKSUMS, (system, machine), (target, hashlib.sha256(data).hexdigest())
    )
    calls = []

    def fetch(request, *, timeout):
        assert timeout == 60
        calls.append(request.full_url)
        if len(calls) == 1:
            raise HTTPError(request.full_url, 403, "Forbidden", {}, None)
        return io.BytesIO(data)

    monkeypatch.setattr(installer, "urlopen", fetch)
    binary = installer.download(tmp_path)
    assert calls == [
        f"https://downloads.rclone.org/{installer.VERSION}/rclone-{installer.VERSION}-{target}.zip",
        f"https://github.com/rclone/rclone/releases/download/{installer.VERSION}/"
        f"rclone-{installer.VERSION}-{target}.zip",
    ]
    assert binary.read_bytes() == payload
    assert binary.stat().st_mode & 0o777 == 0o700


def test_integrity_failure_never_publishes_or_silently_uses_another_mirror(tmp_path, monkeypatch):
    calls = []

    def fetch(request, *, timeout):
        calls.append(request.full_url)
        return io.BytesIO(b"untrusted download")

    monkeypatch.setattr(installer, "urlopen", fetch)
    with pytest.raises(RuntimeError, match="pinned SHA256"):
        installer.download(tmp_path)
    assert len(calls) == 1
    assert list(tmp_path.iterdir()) == []


def test_both_official_sources_unavailable_fail_without_an_executable(tmp_path, monkeypatch):
    def fetch(request, *, timeout):
        raise URLError("network unavailable")

    monkeypatch.setattr(installer, "urlopen", fetch)
    with pytest.raises(RuntimeError, match="official sources") as error:
        installer.download(tmp_path)
    assert "downloads.rclone.org" in str(error.value)
    assert "github.com/rclone/rclone" in str(error.value)
    assert list(tmp_path.iterdir()) == []
