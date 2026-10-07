"""Download checksum-pinned official rclone for Linux clients/CI and Mac agents."""

import argparse
import hashlib
import platform
import shutil
import zipfile
from pathlib import Path
from urllib.request import urlopen

VERSION = "v1.75.1"
CHECKSUMS = {
    ("Linux", "x86_64"): (
        "linux-amd64",
        "982b5aa772841168f8e380f139e9e787b2a105403e32b94da8676a0e1c0a13ab",
    ),
    ("Linux", "aarch64"): (
        "linux-arm64",
        "03f2504174034b6d004152ed7369251c9a9ec1f7e0836eda420f5c7a5ec0dff9",
    ),
    ("Darwin", "arm64"): (
        "osx-arm64",
        "c61d7a371c62bcbbe882c3423aa4b8bf63485c248dd0f692997b8f0c3f6d0c6f",
    ),
    ("Darwin", "x86_64"): (
        "osx-amd64",
        "29253d0288b8fbbac46baad6e5f6add6cb01d462c79f10805bbd4631c4cdf82c",
    ),
}


def download(directory: Path) -> Path:
    selected = CHECKSUMS.get((platform.system(), platform.machine()))
    if selected is None:
        raise RuntimeError("Supported downloads are Linux x86-64/arm64 and macOS arm64/x86-64")
    target, digest = selected
    directory.mkdir(parents=True, mode=0o700, exist_ok=True)
    filename = f"rclone-{VERSION}-{target}.zip"
    archive = directory / filename
    if not archive.exists() or hashlib.sha256(archive.read_bytes()).hexdigest() != digest:
        with urlopen(f"https://downloads.rclone.org/{VERSION}/{filename}", timeout=60) as response:
            data = response.read()
        if hashlib.sha256(data).hexdigest() != digest:
            raise RuntimeError("Official rclone archive differs from the pinned SHA256")
        archive.write_bytes(data)
    executable = directory / "rclone"
    with zipfile.ZipFile(archive) as contents, executable.open("wb") as output:
        # Extract only the known executable; no archive-supplied filesystem paths are used.
        with contents.open(f"rclone-{VERSION}-{target}/rclone") as source:
            shutil.copyfileobj(source, output)
    executable.chmod(0o700)
    return executable


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", required=True, type=Path)
    args = parser.parse_args()
    print(download(args.directory.resolve()))
