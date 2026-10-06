"""Build checksum-pinned official HAProxy for native Linux deployment acceptance."""

import argparse
import hashlib
import shutil
import subprocess
import tarfile
from pathlib import Path
from urllib.request import urlopen

VERSION = "3.4.6"
SHA256 = "791e1815f8af6e8b850a227a9a0a190f3d3478c9e8d38a0f51c98b7f4bfe368b"


def build(directory: Path) -> None:
    directory.mkdir(parents=True, mode=0o700, exist_ok=True)
    archive_path = directory / "haproxy-source.tgz"
    if not archive_path.exists() or hashlib.sha256(archive_path.read_bytes()).hexdigest() != SHA256:
        with urlopen(
            f"https://www.haproxy.org/download/3.4/src/haproxy-{VERSION}.tar.gz", timeout=60
        ) as response:
            data = response.read()
        if hashlib.sha256(data).hexdigest() != SHA256:
            raise RuntimeError("Official HAProxy source differs from the pinned SHA256")
        archive_path.write_bytes(data)
    with tarfile.open(archive_path) as archive:
        archive.extractall(directory, filter="data")
    source = directory / f"haproxy-{VERSION}"
    subprocess.run(
        ["make", "-j4", "-C", str(source), "TARGET=linux-glibc"], check=True, timeout=180
    )
    shutil.copy2(source / "haproxy", directory / "haproxy")
    (directory / "haproxy").chmod(0o700)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", required=True, type=Path)
    build(parser.parse_args().directory.resolve())
