for FL_ROLE in scheduler gateway; do
  flssh "$FL_ROLE" "bash -se -- $FL_ROLE" <<'HOST'
set -o pipefail
source "$HOME/fl-secrets/settings.env"
source "$HOME/fl-secrets/resources.env"
FL_ROLE=$1
sudo apt-get update
sudo apt-get install -y nginx openssh-server git curl jq python3 python3-venv \
  certbot \
  python3-certbot-dns-google python3-certbot-dns-route53 python3-certbot-dns-cloudflare
if [ "$FL_ROLE" = scheduler ]; then
  FL_VOLUME=${FL_AWS_SCHEDULER_VOLUME:-}; FL_DIRS=(postgresql headscale)
else
  FL_VOLUME=${FL_AWS_GATEWAY_VOLUME:-}; FL_DIRS=(fl-transfer)
fi
if [ "$FL_CLOUD" = gcp ]; then
  FL_DEVICE=/dev/disk/by-id/google-fl-data
else
  FL_SERIAL=${FL_VOLUME//-/}
  FL_DEVICE=$(lsblk -dn -o PATH,SERIAL | awk -v serial="$FL_SERIAL" '$2 == serial {print $1}')
fi
test -n "$FL_DEVICE" && test -b "$FL_DEVICE"
sudo install -d -m 755 /srv/fl-data
if ! mountpoint -q /srv/fl-data; then
  FL_FS=$(sudo blkid -s TYPE -o value "$FL_DEVICE" || true)
  if [ -z "$FL_FS" ]; then
    test "$(lsblk -n -o TYPE "$FL_DEVICE" | wc -l)" -eq 1
    test -z "$(sudo blkid -s PTTYPE -o value "$FL_DEVICE" || true)"
    sudo mkfs.ext4 "$FL_DEVICE"
  else
    test "$FL_FS" = ext4
  fi
  FL_UUID=$(sudo blkid -s UUID -o value "$FL_DEVICE")
  if ! grep -q ' /srv/fl-data ' /etc/fstab; then
    printf 'UUID=%s /srv/fl-data ext4 defaults 0 2\n' "$FL_UUID" | sudo tee -a /etc/fstab >/dev/null
  fi
  sudo mount /srv/fl-data
fi
for FL_DIR in "${FL_DIRS[@]}"; do
  sudo install -d -m 755 "/srv/fl-data/$FL_DIR" "/var/lib/$FL_DIR"
  if ! grep -q " /var/lib/$FL_DIR " /etc/fstab; then
    printf '/srv/fl-data/%s /var/lib/%s none bind,x-systemd.requires-mounts-for=/srv/fl-data 0 0\n' \
      "$FL_DIR" "$FL_DIR" | sudo tee -a /etc/fstab >/dev/null
  fi
  mountpoint -q "/var/lib/$FL_DIR" || sudo mount "/var/lib/$FL_DIR"
done
sudo install -d -m 755 /opt/fl /etc/fl
sudo tar -xf "$HOME/fl-secrets/source.tar" -C /opt/fl --no-same-owner
sudo chmod -R a+rX /opt/fl
curl -fsSL https://pixi.sh/install.sh -o /tmp/fl-pixi-install.sh
sudo env PIXI_VERSION="$FL_PIXI_VERSION" PIXI_HOME=/opt/pixi PIXI_BIN_DIR=/usr/local/bin \
  PIXI_NO_PATH_UPDATE=1 bash /tmp/fl-pixi-install.sh
cd /opt/fl
sudo /usr/local/bin/pixi install --locked
sudo chmod -R a+rX /opt/fl/.pixi
if [ "$FL_ROLE" = scheduler ]; then
  id headscale >/dev/null 2>&1 || sudo useradd --system --user-group --home-dir /var/lib/headscale headscale
  id fl-scheduler >/dev/null 2>&1 || sudo useradd --system --user-group --home-dir /nonexistent fl-scheduler
  sudo chown headscale:headscale /var/lib/headscale
  sudo chmod 700 /var/lib/headscale
else
  id fl-transfer >/dev/null 2>&1 || sudo useradd --system --user-group \
    --home-dir /var/lib/fl-transfer --shell /bin/sh fl-transfer
  # A non-password marker allows forced-key login with UsePAM no; password auth remains disabled.
  sudo usermod --password '*' fl-transfer
  sudo chown fl-transfer:fl-transfer /var/lib/fl-transfer
  sudo chmod 700 /var/lib/fl-transfer
fi
HOST
done
