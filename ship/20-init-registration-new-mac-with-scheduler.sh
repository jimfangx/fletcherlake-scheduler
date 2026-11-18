# replace cluster-overrides.yaml, start with shape in examples/cluster.yaml
cd /opt/fl
pixi run fl cluster setup init /path/to/cluster-overrides.yaml \
  --scheduler "https://scheduler.$FL_DOMAIN"
# Enter the enrollment token at the hidden prompt.
pixi run fl cluster setup confirm /opt/fl
pixi run fl cluster status
pixi run fl cluster status --dashboard

# Use fl cluster setup reconfigure /opt/fl --config /path/to/new.yaml for configuration changes. fl cluster restart drains hardware before a Mac reboot; fl cluster destroy permanently retires the registration and preserves retention cleanup. These are lifecycle actions, not routine status commands.
