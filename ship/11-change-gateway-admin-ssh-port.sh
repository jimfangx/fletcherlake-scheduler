flssh gateway 'bash -se' <<'HOST'
printf 'Port 2222\nPasswordAuthentication no\nKbdInteractiveAuthentication no\n' | \
  sudo tee /etc/ssh/sshd_config.d/00-fl-admin.conf >/dev/null
sudo /usr/sbin/sshd -t
sudo install -d -m 755 /etc/systemd/system/ssh.socket.d
printf '[Socket]\nListenStream=\nListenStream=2222\n' | \
  sudo tee /etc/systemd/system/ssh.socket.d/10-fl-admin.conf >/dev/null
sudo systemctl daemon-reload
sudo systemctl restart ssh.socket ssh.service
sudo ss -lntp
HOST
export FL_GATEWAY_SSH_PORT=2222
flssh gateway 'hostname; sudo /usr/sbin/sshd -T | grep "^port "'
flsave
