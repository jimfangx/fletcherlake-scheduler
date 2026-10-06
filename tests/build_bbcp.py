"""Build checksum-pinned official BBCP for Linux CI or a Mac agent/client."""

import argparse
import hashlib
import platform
import shutil
import subprocess
import tarfile
from pathlib import Path
from urllib.request import urlopen

COMMIT = "866ba250b8e7915f23e159a1060038933e829780"
SHA256 = "103292abfc1175285e512fb4cfbb01334d8c208b13b821a244c3de6884a1344a"


def build(directory: Path, openssl_prefix: Path | None = None) -> None:
    system, machine = platform.system(), platform.machine()
    if (system, machine) not in {("Linux", "x86_64"), ("Darwin", "arm64"), ("Darwin", "x86_64")}:
        raise RuntimeError("Supported builds are Linux x86-64 and macOS arm64/x86-64")
    directory.mkdir(parents=True, mode=0o700, exist_ok=True)
    archive_path = directory / "bbcp-source.tgz"
    if not archive_path.exists() or hashlib.sha256(archive_path.read_bytes()).hexdigest() != SHA256:
        with urlopen(
            f"https://codeload.github.com/slaclab/bbcp/tar.gz/{COMMIT}", timeout=60
        ) as response:
            data = response.read()
        if hashlib.sha256(data).hexdigest() != SHA256:
            raise RuntimeError("Official BBCP source differs from the pinned SHA256")
        archive_path.write_bytes(data)
    with tarfile.open(archive_path) as archive:
        archive.extractall(directory, filter="data")
    source = directory / f"bbcp-{COMMIT}"
    architecture = "amd64_linux" if system == "Linux" else f"{machine}_darwin"
    for name in ("bin", "obj"):
        (source / name / architecture).mkdir(parents=True, exist_ok=True)
    # Upstream dispatches through `uname -i`, which returns 'unknown' on some CI
    # hosts. Select its existing x86-64 target explicitly; do not patch the source.
    arguments = ["make", "-j4", "-C", str(source / "src")]
    if system == "Linux":
        arguments += ["makeLinuxx86_64"]
    else:
        if openssl_prefix is None or not (openssl_prefix / "include/openssl/md5.h").is_file():
            raise RuntimeError(
                "macOS builds require --openssl-prefix pointing to installed OpenSSL"
            )
        if any(character.isspace() for character in str(openssl_prefix)):
            raise ValueError("Upstream compiler flags require an OpenSSL prefix without spaces")
        # Native clang and pthread semaphores, bypassing upstream's obsolete 10.5
        # target. Its older source requires C++11 for modern Apple compilers.
        arguments += [
            "doitall",
            "CC=clang++",
            "BB=clang",
            "CFLAGS=-Dunix -D_BSD -D_ALL_SOURCE -D_LARGEFILE_SOURCE -D_FILE_OFFSET_BITS=64 "
            "-DNL_THREADSAFE -D_REENTRANT -DOO_STD -DMACOS -std=gnu++11 -Wno-deprecated",
            "BFLAGS=-Dunix -D_BSD -D_ALL_SOURCE -DMACOS",
            f"INCLUDE=-I. -I{openssl_prefix / 'include'}",
            f"LIBS=-L{openssl_prefix / 'lib'} -lz -lpthread -lcrypto",
        ]
    arguments += [
        f"OSVER={architecture}",
        "MD5HEADER=bbcp_MD5_openssl.h",
        f"OBJMD5=../obj/{architecture}/bbcp_MD5_openssl.o",
    ]
    subprocess.run(arguments, check=True, timeout=180)
    shutil.copy2(source / "bin" / architecture / "bbcp", directory / "bbcp")
    (directory / "bbcp").chmod(0o700)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", required=True, type=Path)
    parser.add_argument("--openssl-prefix", type=Path)
    args = parser.parse_args()
    build(args.directory.resolve(), args.openssl_prefix)
