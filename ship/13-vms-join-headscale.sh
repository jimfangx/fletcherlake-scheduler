for FL_ROLE in scheduler gateway; do
  FL_TAG=scheduler
  if [ "$FL_ROLE" = gateway ]; then FL_TAG=transfer-gateway; fi
  flssh scheduler "sudo -u headscale headscale --config /etc/headscale/config.yaml \
    preauthkeys create --tags tag:$FL_TAG --expiration 10m --output json" \
    | jq -er .key > "$FL_STATE/$FL_ROLE-join-key"
  flput "$FL_ROLE" "$FL_STATE/$FL_ROLE-join-key" fl-secrets/join-key
  flssh "$FL_ROLE" 'bash -se' <<'HOST'
source "$HOME/fl-secrets/settings.env"
chmod 600 "$HOME/fl-secrets/join-key"
sudo tailscale up --login-server "https://headscale.$FL_DOMAIN" \
  --auth-key "file:$HOME/fl-secrets/join-key" --accept-routes=false --accept-dns=true
rm "$HOME/fl-secrets/join-key"
HOST
  rm "$FL_STATE/$FL_ROLE-join-key"
done
FL_SCHEDULER_HEADSCALE_IP=$(flssh scheduler 'tailscale ip -4')
export FL_SCHEDULER_HEADSCALE_IP
FL_GATEWAY_HEADSCALE_IP=$(flssh gateway 'tailscale ip -4')
export FL_GATEWAY_HEADSCALE_IP
flsave
flssh scheduler 'sudo -u headscale headscale --config /etc/headscale/config.yaml \
  apikeys create --expiration 720h --output json' | jq -er . > "$FL_STATE/headscale-api-key"
python3 - "$FL_STATE" <<'PY'
import secrets
import sys
from pathlib import Path
root = Path(sys.argv[1])
for name in ('gateway-control-secret', 'postgres-password'):
    path = root / name
    if not path.exists():
        path.write_text(secrets.token_hex(32) + '\n')
        path.chmod(0o600)
PY
flssh scheduler 'cd /opt/fl && sudo pixi run python -c \
  "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"' \
  > "$FL_STATE/enrollment-encryption-key"
