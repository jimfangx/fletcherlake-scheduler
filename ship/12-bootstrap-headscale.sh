for FL_ROLE in scheduler gateway; do
  flssh "$FL_ROLE" 'bash -se' <<'HOST'
cd /opt/fl
sudo pixi run python tests/download_network_tools.py --directory /tmp/fl-network
sudo install -m 755 /tmp/fl-network/headscale /usr/local/bin/headscale
sudo install -m 755 /tmp/fl-network/tailscale_1.102.4_amd64/tailscale /usr/bin/tailscale
sudo install -m 755 /tmp/fl-network/tailscale_1.102.4_amd64/tailscaled /usr/sbin/tailscaled
sudo install -m 644 /tmp/fl-network/tailscale_1.102.4_amd64/systemd/tailscaled.service \
  /etc/systemd/system/tailscaled.service
sudo install -m 644 /tmp/fl-network/tailscale_1.102.4_amd64/systemd/tailscaled.defaults /etc/default/tailscaled
sudo systemctl daemon-reload
sudo systemctl enable --now tailscaled
HOST
done
flssh scheduler 'bash -se' <<'HOST'
source "$HOME/fl-secrets/settings.env"
source "$HOME/fl-secrets/resources.env"
sudo install -d -m 750 -o root -g headscale /etc/headscale
sudo install -m 640 -o root -g headscale /opt/fl/services/headscale/policy.json /etc/headscale/policy.json
sudo /opt/fl/.pixi/envs/default/bin/python - "$FL_DOMAIN" "$FL_SCHEDULER_NIC_IP" <<'PY'
import sys
from pathlib import Path
import yaml
domain, address = sys.argv[1:]
source = Path('/opt/fl/services/headscale')
config = yaml.safe_load((source / 'config.yaml').read_text())
config['server_url'] = f'https://headscale.{domain}'
Path('/etc/headscale/config.yaml').write_text(yaml.safe_dump(config, sort_keys=False))
text = (source / 'nginx.conf').read_text().replace('PUBLIC_IP', address)
text = text.replace('headscale.example.edu', f'headscale.{domain}')
Path('/etc/nginx/conf.d/fl-bootstrap.conf').write_text(text)
PY
sudo chown root:headscale /etc/headscale/config.yaml
sudo chmod 640 /etc/headscale/config.yaml
sudo rm -f /etc/nginx/sites-enabled/default
sudo install -m 644 /opt/fl/services/headscale/headscale.service /etc/systemd/system/headscale.service
sudo install -d -m 755 /etc/systemd/system/headscale.service.d
printf '[Unit]\nRequiresMountsFor=/var/lib/headscale\n' | \
  sudo tee /etc/systemd/system/headscale.service.d/20-data.conf >/dev/null
# configtest creates the key/database; use the same identity as the service.
# Repair ownership left by an earlier bootstrap that ran configtest as root.
sudo chown -R headscale:headscale /var/lib/headscale
sudo -u headscale headscale --config /etc/headscale/config.yaml configtest
sudo systemctl daemon-reload
sudo systemctl enable headscale
sudo systemctl restart headscale
if ! curl --fail --silent --show-error --retry 15 --retry-connrefused \
  --retry-delay 2 --retry-max-time 60 --max-time 5 http://127.0.0.1:8081/health; then
  sudo systemctl status headscale --no-pager --full || true
  sudo journalctl -u headscale -n 80 --no-pager
  exit 1
fi
sudo nginx -t
sudo systemctl restart nginx
HOST
curl --fail --silent --show-error --retry 15 --retry-connrefused \
  --retry-delay 2 --retry-max-time 60 --max-time 5 "https://headscale.$FL_DOMAIN/health"
