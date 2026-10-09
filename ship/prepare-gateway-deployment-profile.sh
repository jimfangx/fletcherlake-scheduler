flssh gateway 'sudo test -f /etc/fl/transfer-host-ed25519 || \
  sudo ssh-keygen -t ed25519 -f /etc/fl/transfer-host-ed25519 -N ""'
flssh gateway 'sudo cat /etc/fl/transfer-host-ed25519.pub' > "$FL_STATE/gateway-host.pub"
python3 - "$FL_STATE" <<'PY'
import json
import os
import sys
from pathlib import Path
root = Path(sys.argv[1])
d = os.environ['FL_DOMAIN']
key = ' '.join((root / 'gateway-host.pub').read_text().split()[:2])
manifest = {
    'scheduler': {'public_ip': os.environ['FL_SCHEDULER_NIC_IP'],
                  'private_ip': os.environ['FL_SCHEDULER_HEADSCALE_IP'],
                  'hostname': f'scheduler.{d}', 'agent_hostname': f'agent.scheduler.{d}',
                  'headscale_hostname': f'headscale.{d}'},
    'gateway': {'public_ip': os.environ['FL_GATEWAY_NIC_IP'],
                'private_ip': os.environ['FL_GATEWAY_HEADSCALE_IP'],
                'hostname': f'transfer.{d}', 'private_hostname': f'gateway.internal.{d}',
                'host_key': key},
    'license_relay': None,
}
(root / 'deployment.json').write_text(json.dumps(manifest, indent=2) + '\n')
PY
flput scheduler "$FL_STATE/deployment.json" fl-secrets/deployment.json
flssh scheduler 'cd /opt/fl && sudo pixi run fl-deploy \
  /home/ubuntu/fl-secrets/deployment.json --output /home/ubuntu/fl-bundle && \
  sudo chown -R ubuntu:ubuntu /home/ubuntu/fl-bundle'
flget scheduler fl-bundle "$FL_STATE/"
flput gateway "$FL_STATE/fl-bundle/gateway" fl-secrets/gateway-bundle
flssh scheduler 'bash -se' <<'HOST'
FL_BUNDLE="$HOME/fl-bundle"
sudo install -m 640 -o root -g headscale "$FL_BUNDLE/scheduler/headscale.yaml" /etc/headscale/config.yaml
sudo install -m 640 -o root -g headscale "$FL_BUNDLE/scheduler/policy.json" /etc/headscale/policy.json
sudo install -m 644 "$FL_BUNDLE/scheduler/headscale.service" /etc/systemd/system/headscale.service
sudo install -m 644 "$FL_BUNDLE/scheduler/fl-scheduler.service" /etc/systemd/system/fl-scheduler.service
sudo install -d -m 755 /etc/systemd/system/nginx.service.d
sudo install -m 644 "$FL_BUNDLE/scheduler/nginx.service.d/10-headscale.conf" /etc/systemd/system/nginx.service.d/
sudo install -m 644 "$FL_BUNDLE/scheduler/headscale-nginx.conf" /etc/nginx/conf.d/00-fl-headscale.conf
sudo install -m 644 "$FL_BUNDLE/scheduler/scheduler-nginx.conf" /etc/nginx/conf.d/10-fl-scheduler.conf
sudo install -m 600 -o fl-scheduler -g fl-scheduler "$FL_BUNDLE/gateway/public-endpoint.json" /etc/fl/public-endpoint.json
sudo install -m 600 -o fl-scheduler -g fl-scheduler "$FL_BUNDLE/gateway/private-endpoint.json" /etc/fl/private-endpoint.json
sudo rm -f /etc/nginx/conf.d/fl-bootstrap.conf
sudo headscale --config /etc/headscale/config.yaml configtest
sudo systemctl restart headscale
sudo -u headscale headscale --config /etc/headscale/config.yaml policy check -f /etc/headscale/policy.json
sudo nginx -t
sudo systemctl daemon-reload
sudo systemctl restart nginx
HOST
flssh gateway 'bash -se' <<'HOST'
FL_BUNDLE="$HOME/fl-secrets/gateway-bundle"
sudo install -m 644 "$FL_BUNDLE/fl-transfer-gateway.service" /etc/systemd/system/fl-transfer-gateway.service
sudo install -m 600 "$FL_BUNDLE/sshd.conf" /etc/fl/transfer-sshd.conf
sudo install -m 644 "$FL_BUNDLE/transfer-gateway-nginx.conf" /etc/nginx/conf.d/fl-gateway.conf
sudo install -d -m 755 /etc/systemd/system/nginx.service.d
sudo install -m 644 "$FL_BUNDLE/nginx.service.d/10-headscale.conf" /etc/systemd/system/nginx.service.d/
sudo rm -f /etc/nginx/sites-enabled/default
sudo nginx -t
sudo systemctl daemon-reload
sudo systemctl restart nginx
HOST
