for FL_FILE in google-groups.json google-client-id google-client-secret \
  headscale-api-key gateway-control-secret enrollment-encryption-key postgres-password; do
  flput scheduler "$FL_STATE/$FL_FILE" "fl-secrets/$FL_FILE"
done
flssh scheduler 'bash -se' <<'HOST'
source "$HOME/fl-secrets/settings.env"
sudo install -d -m 755 /usr/share/postgresql-common/pgdg
sudo curl -fsSL https://www.postgresql.org/media/keys/ACCC4CF8.asc \
  -o /usr/share/postgresql-common/pgdg/apt.postgresql.org.asc
printf 'deb [signed-by=/usr/share/postgresql-common/pgdg/apt.postgresql.org.asc] https://apt.postgresql.org/pub/repos/apt noble-pgdg main\n' | \
  sudo tee /etc/apt/sources.list.d/pgdg.list >/dev/null
sudo apt-get update
sudo apt-get install -y postgresql-17 postgresql-client-17
sudo systemctl enable --now postgresql
sudo install -m 600 -o fl-scheduler -g fl-scheduler \
  "$HOME/fl-secrets/google-groups.json" /etc/fl/google-groups.json
sudo /opt/fl/.pixi/envs/default/bin/python - "$HOME/fl-secrets" "$HOME/fl-bundle" "$FL_WORKSPACE_DOMAIN" <<'PY'
import os
import re
import sys
from pathlib import Path
from urllib.parse import quote
os.umask(0o077)
secret, bundle = map(Path, sys.argv[1:3])
def read(name):
    return (secret / name).read_text().strip()
password = read('postgres-password')
assert re.fullmatch('[0-9a-f]{64}', password)
# These are fresh-database commands; do not run CREATE ROLE/DB again on an existing deployment.
sql = f"CREATE ROLE fl_scheduler LOGIN PASSWORD '{password}';\nCREATE DATABASE fletcherlake OWNER fl_scheduler;\n"
sql_path = Path('/etc/fl/create-database.sql')
sql_path.write_text(sql)
sql_path.chmod(0o600)
domain = sys.argv[3]
values = {
    'FL_DATABASE_URL': f'postgresql+psycopg://fl_scheduler:{quote(password, safe="")}@127.0.0.1:5432/fletcherlake',
    'FL_GOOGLE_CLIENT_ID': read('google-client-id'),
    'FL_GOOGLE_CLIENT_SECRET': read('google-client-secret'),
    'FL_GOOGLE_GROUPS_BACKEND': 'cloud_identity',
    'FL_GOOGLE_GROUPS_CREDENTIALS': '/etc/fl/google-groups.json',
    'FL_GOOGLE_WORKSPACE_DOMAIN': domain,
    'FL_GOOGLE_USERS_GROUP': f'fl-users@{domain}',
    'FL_GOOGLE_OPERATORS_GROUP': f'fl-operators@{domain}',
    'FL_GOOGLE_ADMINS_GROUP': f'fl-admins@{domain}',
    'FL_HEADSCALE_API_KEY': read('headscale-api-key'),
    'FL_ENROLLMENT_ENCRYPTION_KEY': read('enrollment-encryption-key'),
    'FL_TRANSFER_GATEWAY_CONTROL_SECRET': read('gateway-control-secret'),
}
def quoted(value):
    if any(c in value for c in '\r\n\0'):
        raise ValueError('Environment values must be single-line strings')
    return '"' + value.replace('\\', '\\\\').replace('"', '\\"') + '"'
text = (bundle / 'scheduler/environment.defaults').read_text()
text += ''.join(f'{key}={quoted(value)}\n' for key, value in values.items())
target = Path('/etc/fl/scheduler.env')
target.write_text(text)
target.chmod(0o600)
PY
sudo cat /etc/fl/create-database.sql | sudo -u postgres psql -q --set=ON_ERROR_STOP=1
sudo rm /etc/fl/create-database.sql
cd /opt/fl
sudo pixi run -e web web-install
sudo pixi run -e web web-check
sudo pixi run -e web web-test
sudo pixi run -e web web-build
sudo chmod -R a+rX /opt/fl/services/dashboard/dist
sudo systemd-run --wait --pipe --collect \
  --property=User=fl-scheduler --property=WorkingDirectory=/opt/fl \
  --property=EnvironmentFile=/etc/fl/scheduler.env \
  /opt/fl/.pixi/envs/default/bin/alembic -c /opt/fl/services/scheduler/alembic.ini upgrade head
sudo install -d -m 755 /etc/systemd/system/fl-scheduler.service.d
printf '[Unit]\nRequiresMountsFor=/var/lib/postgresql\nAfter=postgresql.service\nWants=postgresql.service\n' | \
  sudo tee /etc/systemd/system/fl-scheduler.service.d/20-data.conf >/dev/null
sudo systemd-analyze verify /etc/systemd/system/fl-scheduler.service
sudo nginx -t
sudo systemctl daemon-reload
sudo systemctl enable --now fl-scheduler
sudo systemctl reload nginx
HOST
curl --fail "https://scheduler.$FL_DOMAIN/healthz"
