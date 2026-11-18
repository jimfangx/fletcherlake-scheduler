"""Package the checked commit and its pinned lilikoi host code for both cloud VMs."""

import argparse
import io
import subprocess
import tarfile
from pathlib import Path


def archive(repo: Path, revision: str, prefix: str = "") -> bytes:
    return subprocess.check_output(
        ["git", "-C", str(repo), "archive", "--format=tar", f"--prefix={prefix}", revision]
    )


def build_release(repo: Path, revision: str, output: Path) -> None:
    head = subprocess.check_output(["git", "-C", str(repo), "rev-parse", "HEAD"], text=True).strip()
    if revision != head:
        raise ValueError("Release must match the checked-out commit")
    lilikoi = subprocess.check_output(
        ["git", "-C", str(repo), "rev-parse", f"{revision}:packages/lilikoi"], text=True
    ).strip()
    dashboard = repo / "services/dashboard/dist"
    if not (dashboard / "index.html").is_file():
        raise ValueError("Build the dashboard before packaging")
    output.parent.mkdir(parents=True, exist_ok=True)
    with tarfile.open(output, "w:gz") as bundle:
        for source in (
            archive(repo, revision),
            archive(repo / "packages/lilikoi", lilikoi, "packages/lilikoi/"),
        ):
            with tarfile.open(fileobj=io.BytesIO(source)) as git_archive:
                for member in git_archive:
                    if member.name.startswith("packages/lilikoi/pico-sdk"):
                        continue
                    bundle.addfile(
                        member, git_archive.extractfile(member) if member.isfile() else None
                    )
        bundle.add(dashboard, arcname="services/dashboard/dist")
        content = (revision + "\n").encode()
        marker = tarfile.TarInfo("DEPLOYED_REVISION")
        marker.size, marker.mode = len(content), 0o644
        bundle.addfile(marker, io.BytesIO(content))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--revision", required=True)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    build_release(Path(__file__).resolve().parents[2], args.revision, args.output)


if __name__ == "__main__":
    main()
