xcode-select --install

set -euo pipefail
export FL_DOMAIN=REPLACE_WITH_THE_SAME_DEPLOYED_DOMAIN
export FL_PIXI_VERSION=v0.81.0
xcode-select -p
sudo install -d -m 755 -o "$(id -un)" /opt/fl
tar -xf "$HOME/fl-source.tar" -C /opt/fl
curl -fsSL https://pixi.sh/install.sh -o /tmp/fl-pixi-install.sh
sudo env PIXI_VERSION="$FL_PIXI_VERSION" PIXI_HOME=/opt/pixi PIXI_BIN_DIR=/usr/local/bin \
  PIXI_NO_PATH_UPDATE=1 bash /tmp/fl-pixi-install.sh
cd /opt/fl
pixi install --locked
pixi run python tests/download_rclone.py --directory /tmp/fl-rclone
sudo install -d -m 755 /opt/fl-tools
sudo install -m 755 /tmp/fl-rclone/rclone /opt/fl-tools/rclone
export PATH="/opt/fl-tools:/usr/local/bin:$PATH"
tailscale version
