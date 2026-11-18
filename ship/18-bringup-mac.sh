#!/usr/bin/env bash
# Usage: FL_DOMAIN=fl.ucb.bar bash ship/18-bringup-mac.sh SSH_HOST [LOCAL_BOARD_OVERRIDES]
# SSH_HOST may be an alias from ~/.ssh/config. No identity, port or config overrides are used.
# By default, prompts on the Mac create ~/cluster-overrides.yaml; a file is optional.
# This installs a NEW board-hosting Mac agent, including its local UI and fl-client CLI.
# Requires macOS 13+, Command Line Tools, approved/running Tailscale, SSH and sudo access.
if [ -z "${BASH_VERSION:-}" ]; then
  printf 'Run this script with bash.\n' >&2
  return 1 2>/dev/null || exit 1
fi

# Keep options, traps and temporary variables local when sourced by the deployment driver.
(
  set -euo pipefail
  umask 077
  FL_MAC_SSH="${1:-${FL_MAC_SSH:-}}"
  FL_MAC_CONFIG="${2:-${FL_MAC_CONFIG:-}}"
  FL_STATE="${FL_STATE:-$HOME/.local/state/fl-deploy}"
  FL_PIXI_VERSION="${FL_PIXI_VERSION:-v0.81.0}"
  : "${FL_MAC_SSH:?Pass an SSH host/alias or set FL_MAC_SSH}"
  : "${FL_DOMAIN:?Set FL_DOMAIN to the deployed scheduler domain}"
  case "$FL_MAC_SSH" in
    -*|*:*|*[[:space:]]*) printf 'SSH host must be a hostname, user@hostname or SSH config alias.\n' >&2; exit 1 ;;
  esac
  if [ -n "$FL_MAC_CONFIG" ] && [ ! -f "$FL_MAC_CONFIG" ]; then
    printf 'Board overrides not found: %s\n' "$FL_MAC_CONFIG" >&2
    exit 1
  fi
  FL_REPO_ROOT=$(git -C "$(dirname -- "${BASH_SOURCE[0]}")/.." rev-parse --show-toplevel)
  mkdir -p "$FL_STATE"
  FL_MAC_LOCAL_STAGE=$(mktemp -d "$FL_STATE/mac-setup.XXXXXX")
  trap 'rm -rf "$FL_MAC_LOCAL_STAGE"' EXIT

  curl --fail --silent --show-error "https://scheduler.$FL_DOMAIN/healthz"
  if [ ! -f "$FL_REPO_ROOT/packages/lilikoi/host/lilikoi/__init__.py" ]; then
    git -C "$FL_REPO_ROOT" submodule update --init packages/lilikoi
  fi

  # Package current files, including uncommitted edits and nonignored new files (such as
  # new backends). Do not use the old FL_REPO_REV cloud archive. Never follow symlinks or
  # include .git, ignored environments/build outputs, or the firmware-only Pico SDK.
  python3 - "$FL_REPO_ROOT" "$FL_MAC_LOCAL_STAGE/source.tar" "$FL_STATE" <<'PY'
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
    for required in ("pixi.toml", "pixi.lock", "pyproject.toml", "packages/lilikoi/host/lilikoi/__init__.py"):
        bundle.getmember(required)
print(f"Built current Mac sources: {archive} ({archive.stat().st_size:,} bytes)")
PY
  mv "$FL_MAC_LOCAL_STAGE/source.tar" "$FL_STATE/mac-source.tar"

  # Quote settings as shell literals rather than interpolating them into remote commands.
  python3 - "$FL_DOMAIN" "$FL_PIXI_VERSION" "$FL_MAC_CONFIG" > "$FL_MAC_LOCAL_STAGE/setup.sh" <<'PY'
import shlex
import sys
for key, value in zip(("FL_DOMAIN", "FL_PIXI_VERSION", "FL_CONFIG_SUPPLIED"),
                      (sys.argv[1], sys.argv[2], "1" if sys.argv[3] else "0")):
    print(f"{key}={shlex.quote(value)}")
PY
  cat >> "$FL_MAC_LOCAL_STAGE/setup.sh" <<'MAC'
set -euo pipefail
umask 077
FL_STAGE=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
trap 'rm -rf "$FL_STAGE"' EXIT
export PATH="/opt/fl-tools:/opt/homebrew/bin:/usr/local/bin:$PATH"
if [ "$(uname -s)" != Darwin ]; then
  printf 'The target must be a Mac.\n' >&2
  exit 1
fi
FL_MAC_MAJOR=$(sw_vers -productVersion | cut -d. -f1)
if [ "$FL_MAC_MAJOR" -lt 13 ]; then
  printf 'This locked environment requires macOS 13 or later.\n' >&2
  exit 1
fi
xcode-select -p
if command -v tailscale >/dev/null 2>&1; then
  tailscale version
elif [ -x /Applications/Tailscale.app/Contents/MacOS/Tailscale ]; then
  /Applications/Tailscale.app/Contents/MacOS/Tailscale version
else
  printf 'Install, approve and start the native Tailscale app before deployment.\n' >&2
  exit 1
fi
FL_CONFIG="$HOME/cluster-overrides.yaml"
if [ "$FL_CONFIG_SUPPLIED" = 1 ]; then
  FL_CONFIG="$FL_STAGE/cluster-overrides.yaml"
fi
sudo -v
if sudo launchctl print system/edu.berkeley.fletcherlake.agent >/dev/null 2>&1 ||
   sudo test -f '/Library/Application Support/fl/credentials.json'; then
  printf 'This Mac is already enrolled. Use ship/19-2-update-existing-mac.sh SSH_HOST to update it.\n' >&2
  printf 'If enrollment succeeded but installation was interrupted, run setup confirm /opt/fl on the Mac.\n' >&2
  exit 1
fi
sudo install -d -m 755 /opt/fl
sudo tar -xf "$FL_STAGE/source.tar" -C /opt/fl
sudo chmod -R a+rX /opt/fl
# Keep installed tools readable by the operator; staging stays private (created mode 0700).
umask 022
curl --fail --silent --show-error --location https://pixi.sh/install.sh -o "$FL_STAGE/pixi-install.sh"
sudo env PIXI_VERSION="$FL_PIXI_VERSION" PIXI_HOME=/opt/pixi PIXI_BIN_DIR=/usr/local/bin \
  PIXI_NO_PATH_UPDATE=1 bash "$FL_STAGE/pixi-install.sh"
cd /opt/fl
sudo /usr/local/bin/pixi install --locked
FL_PYTHON=/opt/fl/.pixi/envs/default/bin/python
FL_CLI=/opt/fl/.pixi/envs/default/bin/fl
if [ "$FL_CONFIG_SUPPLIED" != 1 ]; then
  "$FL_PYTHON" -m fl_cli.mac_inventory "$FL_CONFIG"
fi
# Validate the supplied board inventory before installing hardware tools or enrolling.
FL_NEEDS_LILIKOI=$("$FL_PYTHON" - "$FL_CONFIG" <<'PY'
from pathlib import Path
import sys
from fl.macos.inventory import inventory
config = inventory(Path(sys.argv[1]))
print("yes" if any(b and b.backend == "lilikoi" for b in config.boards) else "no")
PY
)
"$FL_PYTHON" tests/download_rclone.py --directory "$FL_STAGE/rclone"
sudo install -d -m 755 /opt/fl-tools
sudo install -m 755 "$FL_STAGE/rclone/rclone" /opt/fl-tools/rclone
/opt/fl-tools/rclone version
if [ "$FL_NEEDS_LILIKOI" = yes ]; then
  if ! command -v brew >/dev/null 2>&1; then
    printf 'Install Homebrew before deploying a lilikoi board, then rerun this script.\n' >&2
    exit 1
  fi
  FL_BREW=$(command -v brew)
  FL_BREW_OWNER=$(stat -f '%Su' "$("$FL_BREW" --repository)")
  if [ "$FL_BREW_OWNER" = root ]; then
    printf 'Homebrew is root-owned; repair its installation before continuing.\n' >&2
    exit 1
  fi
  sudo -H -u "$FL_BREW_OWNER" "$FL_BREW" tap riscv-software-src/riscv
  sudo -H -u "$FL_BREW_OWNER" "$FL_BREW" install open-ocd riscv-software-src/riscv/riscv-isa-sim
  FL_FESVR_PREFIX=$("$FL_BREW" --prefix riscv-isa-sim)
  test -f "$FL_FESVR_PREFIX/include/fesvr/tsi.h"
  test -f "$FL_FESVR_PREFIX/include/fesvr/htif.h"
  sudo make -C /opt/fl/packages/lilikoi/host/uart_tsi RISCV="$FL_FESVR_PREFIX"
fi
printf '\nCreate an enrollment ticket at https://scheduler.%s/admin/clusters.\n' "$FL_DOMAIN"
printf 'Enter it at the hidden prompt below.\n'
sudo env PATH="$PATH" "$FL_CLI" cluster setup init "$FL_CONFIG" --scheduler "https://scheduler.$FL_DOMAIN"
sudo env PATH="$PATH" "$FL_CLI" cluster setup confirm /opt/fl
sudo "$FL_CLI" cluster status
printf '\nAgent installed. Local dashboard: sudo /opt/fl/.pixi/envs/default/bin/fl cluster status --dashboard\n'
printf 'Remote submission CLI: /opt/fl/.pixi/envs/default/bin/fl-client\n'
MAC

  FL_MAC_REMOTE_STAGE=$(ssh "$FL_MAC_SSH" 'umask 077; mktemp -d /tmp/fl-mac-setup.XXXXXX')
  if [[ ! "$FL_MAC_REMOTE_STAGE" =~ ^/tmp/fl-mac-setup\.[[:alnum:]]+$ ]]; then
    printf 'Unexpected remote staging path: %s\n' "$FL_MAC_REMOTE_STAGE" >&2
    exit 1
  fi
  scp "$FL_STATE/mac-source.tar" "$FL_MAC_SSH:$FL_MAC_REMOTE_STAGE/source.tar"
  scp "$FL_MAC_LOCAL_STAGE/setup.sh" "$FL_MAC_SSH:$FL_MAC_REMOTE_STAGE/setup.sh"
  if [ -n "$FL_MAC_CONFIG" ]; then
    scp "$FL_MAC_CONFIG" "$FL_MAC_SSH:$FL_MAC_REMOTE_STAGE/cluster-overrides.yaml"
  fi
  # Run a stored script with a real terminal: sudo and enrollment prompts read the SSH TTY.
  # Standard ssh/scp retain ~/.ssh/config User, Port, IdentityFile, ProxyJump, etc.
  ssh -t "$FL_MAC_SSH" "bash '$FL_MAC_REMOTE_STAGE/setup.sh'"
)
