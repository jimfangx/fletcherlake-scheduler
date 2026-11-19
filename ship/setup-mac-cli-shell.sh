#!/usr/bin/env bash
# Run as the Mac's SSH/login user after /opt/fl's environment has been installed.
set -euo pipefail
FL_CLI=/opt/fl/.pixi/envs/default/bin/fl
test -x "$FL_CLI"
sudo install -d -m 755 /usr/local/bin
for FL_COMMAND in fl fletcherlake; do
  FL_LAUNCHER="/usr/local/bin/$FL_COMMAND"
  if [ -e "$FL_LAUNCHER" ] || [ -L "$FL_LAUNCHER" ]; then
    if [ "$(readlink "$FL_LAUNCHER" || true)" != "$FL_CLI" ]; then
      printf 'Cannot install %s: %s already belongs to another command.\n' "$FL_COMMAND" "$FL_LAUNCHER" >&2
      exit 1
    fi
  else
    sudo ln -s "$FL_CLI" "$FL_LAUNCHER"
  fi
done
/opt/fl/.pixi/envs/default/bin/python - <<'PY'
import os
import shutil
import stat
import tempfile
from pathlib import Path

rc = (Path(os.environ.get("ZDOTDIR") or Path.home()) / ".zshrc").resolve()
start, end = "# >>> Fletcherlake CLI >>>", "# <<< Fletcherlake CLI <<<"
block = f'{start}\nexport PATH="/usr/local/bin:$PATH"\n{end}\n'
original = rc.read_text() if rc.exists() else ""
if start in original or end in original:
    if original.count(start) != 1 or original.count(end) != 1:
        raise RuntimeError("Repair the incomplete Fletcherlake block in " + str(rc))
    first, last = original.index(start), original.index(end) + len(end)
    if last <= first:
        raise RuntimeError("Repair the incomplete Fletcherlake block in " + str(rc))
    updated = original[:first] + block.rstrip("\n") + original[last:]
else:
    updated = original + ("\n" if original and not original.endswith("\n") else "") + "\n" + block
if updated != original:
    mode = stat.S_IMODE(rc.stat().st_mode) if rc.exists() else 0o600
    backup = rc.with_name(rc.name + ".before-fletcherlake")
    if rc.exists() and not backup.exists():
        shutil.copy2(rc, backup)
    fd, name = tempfile.mkstemp(prefix=".fl-zshrc-", dir=rc.parent)
    try:
        with os.fdopen(fd, "w") as stream:
            os.fchmod(stream.fileno(), mode)
            stream.write(updated)
        os.replace(name, rc)
    finally:
        Path(name).unlink(missing_ok=True)
print(f"CLI commands installed: fl, fletcherlake. Open a new terminal or source {rc}")
PY
