"""Download pinned official Linux test tools; authenticate bytes before extraction or execution."""

import argparse
import hashlib
import tarfile
from pathlib import Path
from urllib.request import urlopen

TOOLS = (
    (
        "headscale",
        "https://github.com/juanfont/headscale/releases/download/v0.29.4/headscale_0.29.4_linux_amd64",
        "212ed0a884c0d3541e094c4bebbe94397df6f4e01bd3d7f059c520cb55e0d757",
    ),
    (
        "tailscale.tgz",
        "https://pkgs.tailscale.com/stable/tailscale_1.102.4_amd64.tgz",
        "50748df1045e60b5b695f19f4c56b0da36c019948b440fb456b6584a50f0d8b9",
    ),
)


def download(directory: Path) -> None:
    directory.mkdir(parents=True, mode=0o700, exist_ok=True)
    for name, url, expected in TOOLS:
        target = directory / name
        if target.exists() and hashlib.sha256(target.read_bytes()).hexdigest() == expected:
            continue
        with urlopen(url, timeout=60) as response:
            data = response.read()
        if hashlib.sha256(data).hexdigest() != expected:
            raise RuntimeError(f"Official {name} artifact does not match the pinned SHA256")
        target.write_bytes(data)
    (directory / "headscale").chmod(0o700)
    with tarfile.open(directory / "tailscale.tgz") as archive:
        archive.extractall(directory, filter="data")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", required=True, type=Path)
    download(parser.parse_args().directory.resolve())
