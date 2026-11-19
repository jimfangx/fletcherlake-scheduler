#!/usr/bin/env bash
# Invoked over IAP by deploy.py as root on an EXISTING provisioned cloud VM.
set -euo pipefail
umask 077
FL_PHASE=${1:?phase}
FL_ROLE=${2:?role}
FL_RELEASE_ID=${3:?release id}
FL_REVISION=${4:?revision}
FL_DIGEST=${5:?archive sha256}
[[ "$FL_ROLE" = scheduler || "$FL_ROLE" = gateway ]]
[[ "$FL_RELEASE_ID" =~ ^[a-f0-9]{40}-[0-9]+-[0-9]+$ ]]
[[ "$FL_REVISION" =~ ^[a-f0-9]{40}$ ]]
[[ "$FL_DIGEST" =~ ^[a-f0-9]{64}$ ]]
[[ "$FL_RELEASE_ID" = "$FL_REVISION"-* ]]
test "$(id -u)" -eq 0
FL_RELEASE="/opt/fl-releases/$FL_RELEASE_ID"
FL_BACKUP="/var/backups/fl-cloud/$FL_RELEASE_ID"
FL_UNIT=fl-scheduler
FL_ENV=/etc/fl/scheduler.env
FL_PORT=8080
if [ "$FL_ROLE" = gateway ]; then
  FL_UNIT=fl-transfer-gateway
  FL_ENV=/etc/fl/transfer-gateway.env
  FL_PORT=8081
fi
case "$FL_PHASE" in
  prepare)
    test -f "$FL_ENV"
    test -x /usr/local/bin/pixi
    test -d /opt/fl
    systemctl is-active --quiet "$FL_UNIT"
    # All installation work finishes before either live service is stopped.
    printf '%s  /home/fl-deploy/cloud-release.tar.gz\n' "$FL_DIGEST" | sha256sum --check --status
    test ! -e "$FL_RELEASE"
    install -d -m 755 "$FL_RELEASE"
    tar -xzf /home/fl-deploy/cloud-release.tar.gz -C "$FL_RELEASE" --no-same-owner
    test "$(cat "$FL_RELEASE/DEPLOYED_REVISION")" = "$FL_REVISION"
    chmod -R a+rX "$FL_RELEASE"
    cd "$FL_RELEASE"
    (umask 022; /usr/local/bin/pixi install --locked)
    chmod -R a+rX "$FL_RELEASE/.pixi"
    if [ "$FL_ROLE" = scheduler ]; then
      test -f "$FL_RELEASE/services/dashboard/dist/index.html"
      runuser -u fl-scheduler -- "$FL_RELEASE/.pixi/envs/default/bin/python" -c 'import fl_scheduler'
    else
      systemctl is-active --quiet fl-transfer-sshd
      runuser -u fl-transfer -- "$FL_RELEASE/.pixi/envs/default/bin/python" -c 'import fl_gateway'
    fi
    nginx -t
    ;;
  stop)
    if [ "$FL_ROLE" = scheduler ] && systemctl cat fl-headscale-key-rotation.timer >/dev/null 2>&1; then
      systemctl stop fl-headscale-key-rotation.timer fl-headscale-key-rotation.service
    fi
    systemctl stop "$FL_UNIT"
    if [ "$FL_ROLE" = gateway ]; then systemctl stop fl-transfer-sshd; fi
    ;;
  backup)
    if systemctl is-active --quiet "$FL_UNIT"; then
      printf 'Stop %s before backing up.\n' "$FL_UNIT" >&2; exit 1
    fi
    install -d -m 700 "$FL_BACKUP"
    test ! -f "$FL_BACKUP/backup-complete"
    cp -a /etc/fl "$FL_BACKUP/etc-fl"
    if [ "$FL_ROLE" = scheduler ]; then
      runuser -u postgres -- pg_dump -Fc fletcherlake > "$FL_BACKUP/database.dump"
    else
      # Use SQLite's backup API rather than copying a database with possible WAL pages.
      systemd-run --wait --pipe --collect --property="EnvironmentFile=$FL_ENV" \
        /usr/bin/python3 -c 'import os,sqlite3,sys; from pathlib import Path; source=sqlite3.connect(str(Path(os.environ["FL_GATEWAY_ROOT"])/"gateway.db")); dest=sqlite3.connect(sys.argv[1]); source.backup(dest); dest.close(); source.close()' \
        "$FL_BACKUP/gateway.db"
    fi
    touch "$FL_BACKUP/backup-complete"
    ;;
  activate)
    test -f "$FL_BACKUP/backup-complete"
    if systemctl is-active --quiet "$FL_UNIT"; then
      printf 'Stop %s before activating a release.\n' "$FL_UNIT" >&2; exit 1
    fi
    if [ -L /opt/fl ]; then
      readlink -f /opt/fl > "$FL_BACKUP/previous-release"
    else
      FL_PREVIOUS="/opt/fl-releases/legacy-$FL_RELEASE_ID"
      test ! -e "$FL_PREVIOUS"
      mv /opt/fl "$FL_PREVIOUS"
      printf '%s\n' "$FL_PREVIOUS" > "$FL_BACKUP/previous-release"
    fi
    ln -s "$FL_RELEASE" "/opt/.fl-next-$FL_RELEASE_ID"
    mv -Tf "/opt/.fl-next-$FL_RELEASE_ID" /opt/fl
    ;;
  migrate)
    test "$FL_ROLE" = scheduler
    test "$(readlink -f /opt/fl)" = "$FL_RELEASE"
    systemd-run --wait --pipe --collect --property=User=fl-scheduler \
      --property=WorkingDirectory=/opt/fl --property="EnvironmentFile=$FL_ENV" \
      /opt/fl/.pixi/envs/default/bin/alembic -c /opt/fl/services/scheduler/alembic.ini upgrade head
    ;;
  start)
    test "$(readlink -f /opt/fl)" = "$FL_RELEASE"
    if [ "$FL_ROLE" = scheduler ]; then
      /opt/fl/.pixi/envs/default/bin/python /opt/fl/ship/cloud/ssh_relay_config.py /opt/fl
    fi
    systemctl start "$FL_UNIT"
    curl --fail --silent --show-error --retry 20 --retry-connrefused --retry-delay 2 \
      --retry-max-time 90 --max-time 5 "http://127.0.0.1:$FL_PORT/healthz"
    if [ "$FL_ROLE" = gateway ]; then
      /usr/sbin/sshd -t -f /etc/fl/transfer-sshd.conf
      systemctl start fl-transfer-sshd
      systemctl is-active --quiet fl-transfer-sshd
    fi
    systemctl is-active --quiet "$FL_UNIT"
    test "$(cat /opt/fl/DEPLOYED_REVISION)" = "$FL_REVISION"
    if [ "$FL_ROLE" = scheduler ]; then
      bash /opt/fl/ship/cloud/install-headscale-key-rotation.sh
    fi
    ;;
  *) printf 'Unknown phase: %s\n' "$FL_PHASE" >&2; exit 2 ;;
esac
printf '%s: %s completed for %s\n' "$FL_ROLE" "$FL_PHASE" "$FL_REVISION"
