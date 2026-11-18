flput gateway "$FL_STATE/gateway-control-secret" fl-secrets/gateway-control-secret
flssh gateway 'bash -se' <<'HOST'
cd /opt/fl
sudo install -m 600 -o fl-transfer -g fl-transfer \
  "$HOME/fl-secrets/gateway-control-secret" /var/lib/fl-transfer/control-secret
sudo install -m 600 "$HOME/fl-secrets/gateway-bundle/environment.defaults" /etc/fl/transfer-gateway.env
sudo tee /etc/systemd/system/fl-transfer-sshd.service >/dev/null <<'UNIT'
[Unit]
Description=Fletcherlake dedicated transfer SSH daemon
After=network-online.target tailscaled.service fl-transfer-gateway.service
Wants=network-online.target tailscaled.service fl-transfer-gateway.service
RequiresMountsFor=/var/lib/fl-transfer
[Service]
Type=simple
ExecStartPre=/usr/bin/install -d -m 0755 /run/sshd
ExecStartPre=/usr/sbin/sshd -t -f /etc/fl/transfer-sshd.conf
ExecStart=/usr/sbin/sshd -D -f /etc/fl/transfer-sshd.conf
Restart=on-failure
RestartSec=5
[Install]
WantedBy=multi-user.target
UNIT
sudo install -d -m 755 /etc/systemd/system/fl-transfer-gateway.service.d
printf '[Unit]\nRequiresMountsFor=/var/lib/fl-transfer\n' | \
  sudo tee /etc/systemd/system/fl-transfer-gateway.service.d/20-data.conf >/dev/null
sudo /usr/sbin/sshd -t -f /etc/fl/transfer-sshd.conf
sudo systemd-analyze verify /etc/systemd/system/fl-transfer-gateway.service \
  /etc/systemd/system/fl-transfer-sshd.service
sudo systemctl daemon-reload
sudo systemctl enable --now fl-transfer-gateway fl-transfer-sshd
sudo systemctl restart nginx
sudo ss -lntp
HOST
flssh gateway 'hostname'   # must connect to management port 2222 successfully
