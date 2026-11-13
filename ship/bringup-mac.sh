# Before enrollment, verify the selected cloud scheduler from your operator shell:

flssh scheduler 'systemctl is-active headscale fl-scheduler nginx'
curl --fail "https://scheduler.$FL_DOMAIN/healthz"

# copy source code tar to mac
FL_MAC_SSH=yf328@bwrc-lab04.eecs.berkeley.edu
scp "$FL_STATE/source.tar" "$FL_MAC_SSH:fl-source.tar"
