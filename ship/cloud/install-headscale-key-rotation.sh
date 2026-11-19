#!/usr/bin/env bash
# Run as root on the scheduler after installation, or from a staged source directory.
set -euo pipefail
FL_SOURCE=${1:-/opt/fl}
test "$(id -u)" -eq 0
install -d -m 755 /usr/local/libexec
install -m 644 "$FL_SOURCE/services/headscale/rotate-api-key.py" /usr/local/libexec/fl-rotate-headscale-key.py
for FL_EXTENSION in service timer; do
  install -m 644 "$FL_SOURCE/services/headscale/fl-headscale-key-rotation.$FL_EXTENSION" \
    "/etc/systemd/system/fl-headscale-key-rotation.$FL_EXTENSION"
done
systemd-analyze verify /etc/systemd/system/fl-headscale-key-rotation.service \
  /etc/systemd/system/fl-headscale-key-rotation.timer
systemctl daemon-reload
systemctl enable --now fl-headscale-key-rotation.timer
