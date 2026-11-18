#!/usr/bin/env bash
# Update an EXISTING, running Mac agent from this checkout; SSH config aliases are supported.
# Usage: bash ship/19-2-update-existing-mac.sh SSH_HOST
if [ -z "${BASH_VERSION:-}" ]; then
  printf 'Run this script with bash.\n' >&2
  return 1 2>/dev/null || exit 1
fi
if [ "${1:-}" = --help ] || [ "${1:-}" = -h ]; then
  printf 'Usage: bash ship/19-2-update-existing-mac.sh SSH_HOST\n'
  printf 'Or set FL_MAC_SSH. Updates /opt/fl and preserves enrollment and board configuration.\n'
  return 0 2>/dev/null || exit 0
fi
(
  set -euo pipefail
  umask 077
  FL_MAC_SSH="${1:-${FL_MAC_SSH:-}}"
  FL_STATE="${FL_STATE:-$HOME/.local/state/fl-deploy}"
  : "${FL_MAC_SSH:?Pass an SSH host/alias or set FL_MAC_SSH}"
  if [ "$#" -gt 1 ]; then
    printf 'Usage: bash ship/19-2-update-existing-mac.sh SSH_HOST\n' >&2
    exit 1
  fi
  case "$FL_MAC_SSH" in
    -*|*:*|*[[:space:]]*) printf 'SSH host must be a hostname, user@hostname or SSH config alias.\n' >&2; exit 1 ;;
  esac
  FL_REPO_ROOT=$(git -C "$(dirname -- "${BASH_SOURCE[0]}")/.." rev-parse --show-toplevel)
  mkdir -p "$FL_STATE"
  FL_MAC_UPDATE_STAGE=$(mktemp -d "$FL_STATE/mac-update.XXXXXX")
  FL_MAC_REMOTE_STAGE=
  cleanup() {
    FL_MAC_UPDATE_RESULT=$?
    trap - EXIT
    if [ -n "$FL_MAC_REMOTE_STAGE" ]; then
      ssh "$FL_MAC_SSH" "rm -rf '$FL_MAC_REMOTE_STAGE'" || true
    fi
    rm -rf "$FL_MAC_UPDATE_STAGE"
    exit "$FL_MAC_UPDATE_RESULT"
  }
  trap cleanup EXIT
  if [ ! -f "$FL_REPO_ROOT/packages/lilikoi/host/lilikoi/__init__.py" ]; then
    git -C "$FL_REPO_ROOT" submodule update --init packages/lilikoi
  fi

  # Package current files, including uncommitted edits and nonignored new files (such as
  # new backends). Do not use the old FL_REPO_REV cloud archive. Never follow symlinks or
  # include .git, ignored environments/build outputs, or the firmware-only Pico SDK.
  python3 - "$FL_REPO_ROOT" "$FL_MAC_UPDATE_STAGE/source.tar" "$FL_STATE" <<'PY'
from pathlib import Path
import subprocess
import sys
import tarfile

root, archive, state = map(Path, sys.argv[1:])
root, state = root.resolve(), state.resolve()

def files(repo):
    result = subprocess.check_output([
        "git", "-C", str(repo), "ls-files", "--cached", "--others", "--exclude-standard", "-z"
    ])
    return sorted(set(Path(p.decode("utf-8", "surrogateescape")) for p in result.split(b"\0") if p))

paths = []
for relative in files(root):
    if relative == Path("packages/lilikoi"):
        paths.extend(Path("packages/lilikoi") / child for child in files(root / relative))
    else:
        paths.append(relative)
with tarfile.open(archive, "w", dereference=False) as bundle:
    for relative in sorted(set(paths)):
        if relative.parts[:3] == ("packages", "lilikoi", "pico-sdk"):
            continue
        if ".git" in relative.parts:
            continue
        source = root / relative
        # The state directory may be inside this checkout; never archive deployment secrets.
        if source.is_relative_to(state):
            continue
        if source.is_file() or source.is_symlink():
            bundle.add(source, arcname=str(relative), recursive=False)
    for required in ("pixi.toml", "pixi.lock", "pyproject.toml", "ship/mac-update.py", "packages/lilikoi/host/lilikoi/__init__.py"):
        bundle.getmember(required)
print(f"Built current Mac sources: {archive} ({archive.stat().st_size:,} bytes)")
PY
  mv "$FL_MAC_UPDATE_STAGE/source.tar" "$FL_STATE/mac-update-source.tar"
  cat > "$FL_MAC_UPDATE_STAGE/update.sh" <<'MAC'
set -euo pipefail
umask 077
FL_STAGE=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
export PATH="/opt/fl-tools:/opt/homebrew/bin:/usr/local/bin:$PATH"
if [ "$(uname -s)" != Darwin ]; then
  printf 'The target must be a Mac.\n' >&2
  exit 1
fi
if [ "$(sw_vers -productVersion | cut -d. -f1)" -lt 13 ]; then
  printf 'This locked environment requires macOS 13 or later.\n' >&2
  exit 1
fi
xcode-select -p
sudo -v
sudo test -f '/Library/Application Support/fl/credentials.json'
sudo test -f '/Library/Application Support/fl/cluster.yaml'
sudo test -f /Library/LaunchDaemons/edu.berkeley.fletcherlake.agent.plist
sudo launchctl print system/edu.berkeley.fletcherlake.agent >/dev/null
# Recover an interrupted installation with setup confirm before attempting an update.
sudo /opt/fl/.pixi/envs/default/bin/fl cluster status
test -x /usr/local/bin/pixi
sudo install -d -m 755 /opt/fl-releases
FL_RELEASE=$(sudo mktemp -d /opt/fl-releases/mac-update.XXXXXX)
printf '\nPreparing release: %s\n' "$FL_RELEASE"
printf 'Active jobs will be interrupted during activation; queued jobs are preserved.\n'
sudo tar -xf "$FL_STAGE/source.tar" -C "$FL_RELEASE"
sudo chmod -R a+rX "$FL_RELEASE"
# Prepare the locked environment while the current agent continues serving.
cd "$FL_RELEASE"
sudo sh -c 'umask 022; exec /usr/local/bin/pixi install --locked --manifest-path "$1/pixi.toml"' sh "$FL_RELEASE"
FL_PYTHON="$FL_RELEASE/.pixi/envs/default/bin/python"
FL_NEEDS_LILIKOI=$(sudo "$FL_PYTHON" - <<'PY'
from fl_agent.configuration import DEFAULT_STATE_ROOT, load_config
config = load_config(DEFAULT_STATE_ROOT)
print("yes" if any(b and b.backend == "lilikoi" for b in config.boards) else "no")
PY
)
if [ "$FL_NEEDS_LILIKOI" = yes ]; then
  FL_BREW=$(command -v brew)
  FL_FESVR_PREFIX=$("$FL_BREW" --prefix riscv-isa-sim)
  test -f "$FL_FESVR_PREFIX/include/fesvr/tsi.h"
  test -f "$FL_FESVR_PREFIX/include/fesvr/htif.h"
  sudo make -C "$FL_RELEASE/packages/lilikoi/host/uart_tsi" RISCV="$FL_FESVR_PREFIX"
  sudo chmod -R a+rX "$FL_RELEASE/packages/lilikoi/host/uart_tsi"
fi
sudo "$FL_PYTHON" -c 'from fl import ClusterSetup; import fl_agent.daemon; import fl_client'
# The helper drains hardware before stopping launchd and switching /opt/fl. It holds the
# lifecycle management lock throughout activation and confirmation. No enrollment is run.
if ! sudo env PATH="$PATH" "$FL_PYTHON" "$FL_RELEASE/ship/mac-update.py" "$FL_RELEASE"; then
  printf '\nUpdate incomplete. Release retained at %s; inspect the error above.\n' "$FL_RELEASE" >&2
  printf 'After correcting the cause, recover on the Mac with:\n' >&2
  printf '  sudo /opt/fl/.pixi/envs/default/bin/fl cluster setup confirm /opt/fl\n' >&2
  exit 1
fi
sudo /opt/fl/.pixi/envs/default/bin/fl cluster status
printf '\nMac agent updated: %s\n' "$FL_RELEASE"
printf 'Previous source/environment retained; metadata backup: %s/previous-installation\n' "$FL_RELEASE"
MAC

  FL_MAC_REMOTE_STAGE=$(ssh "$FL_MAC_SSH" 'umask 077; mktemp -d /tmp/fl-mac-update.XXXXXX')
  if [[ ! "$FL_MAC_REMOTE_STAGE" =~ ^/tmp/fl-mac-update\.[[:alnum:]]+$ ]]; then
    printf 'Unexpected remote staging path: %s\n' "$FL_MAC_REMOTE_STAGE" >&2
    FL_MAC_REMOTE_STAGE=
    exit 1
  fi
  scp "$FL_STATE/mac-update-source.tar" "$FL_MAC_SSH:$FL_MAC_REMOTE_STAGE/source.tar"
  scp "$FL_MAC_UPDATE_STAGE/update.sh" "$FL_MAC_SSH:$FL_MAC_REMOTE_STAGE/update.sh"
  ssh -t "$FL_MAC_SSH" "bash '$FL_MAC_REMOTE_STAGE/update.sh'"
)
