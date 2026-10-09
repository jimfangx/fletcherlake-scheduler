#!/usr/bin/env bash
# Source this file inside a dedicated Bash deployment shell.
# Check before changing shell options: zsh's prompt may fail with nounset enabled.
if [ -z "${BASH_VERSION:-}" ]; then
  printf 'Deployment requires Bash. Run bash --noprofile --norc, then source ship/env-vars.sh from the repository root.\n' >&2
  return 1 2>/dev/null || exit 1
fi

set -euo pipefail
umask 077
export FL_STATE="${FL_STATE:-$HOME/.local/state/fl-deploy}"
mkdir -p "$FL_STATE"
chmod 700 "$FL_STATE"
if [ ! -f "$FL_STATE/settings.env" ]; then
cat > "$FL_STATE/settings.env" <<'ENV'
export FL_CLOUD=gcp                         # gcp or aws
export FL_DNS_PROVIDER=cloudflare            # cloudflare, gcp or aws; independent of VM hosting
export FL_DOMAIN=fl.ucb.bar
export FL_CF_ZONE_ID=991e38862be4e6bf9a55f8188d7ba68d  # Only for Cloudflare DNS; usually the parent domain's zone  - ucb.bar
export FL_VPC_CIDR=10.80.0.0/16
export FL_SUBNET_CIDR=10.80.0.0/24
export FL_SCHEDULER_NIC_IP=10.80.0.10
export FL_GATEWAY_NIC_IP=10.80.0.11
export FL_PIXI_VERSION=v0.81.0
export FL_ADMIN_CIDR=128.32.0.0/16 # BWRC is under 128.32.0.0, example: 128.32.62.90 - https://berkeley.service-now.com/kb/en/uc-berkeley-campus-networks?id=kb_article_view&sysparm_article=KB0011960
export FL_REPO_REV=c22218bc22cb3d5c81c8ad5feabc384d3727ef4d # https://github.com/jimfangx/fletcherlake-scheduler/tree/c22218bc22cb3d5c81c8ad5feabc384d3727ef4d
export FL_DEPLOY_TOKEN=fl-bringup-20261008
export FL_GOOGLE_PROJECT=fletcherlake
export FL_WORKSPACE_DOMAIN=berkeley.edu # Domain of the humans' Google accounts
export FL_GOOGLE_USERS_GROUP=fl-users@lists.berkeley.edu
export FL_GOOGLE_OPERATORS_GROUP=fl-operators@lists.berkeley.edu
export FL_GOOGLE_ADMINS_GROUP=fl-admins@lists.berkeley.edu
export FL_GCP_PROJECT=fletcherlake
export FL_GCP_REGION=us-west2
export FL_GCP_ZONE=us-west2-a
export FL_GCP_DNS_ZONE=REPLACE_WITH_EXISTING_MANAGED_ZONE_NAME  # Only for GCP DNS
export AWS_PROFILE=fl-admin
export AWS_DEFAULT_REGION=us-west-2
export FL_AWS_AZ=us-west-2a
export FL_AWS_ZONE_ID=REPLACE_WITH_EXISTING_ROUTE53_ZONE_ID  # Only for AWS DNS
ENV
fi
"${EDITOR:-vi}" "$FL_STATE/settings.env"
source "$FL_STATE/settings.env"
python3 - <<'PYCHECK'
import ipaddress
import os
import re
vpc = ipaddress.IPv4Network(os.environ['FL_VPC_CIDR'])
subnet = ipaddress.IPv4Network(os.environ['FL_SUBNET_CIDR'])
assert subnet.subnet_of(vpc)
assert not subnet.overlaps(ipaddress.IPv4Network('100.64.0.0/10'))
for key in ('FL_SCHEDULER_NIC_IP', 'FL_GATEWAY_NIC_IP'):
    ip = ipaddress.IPv4Address(os.environ[key])
    assert ip in subnet and int(ip) > int(subnet.network_address) + 3
    assert ip != subnet.broadcast_address
assert os.environ['FL_SCHEDULER_NIC_IP'] != os.environ['FL_GATEWAY_NIC_IP']
ipaddress.IPv4Network(os.environ['FL_ADMIN_CIDR'])
assert re.fullmatch('[0-9a-f]{40}', os.environ['FL_REPO_REV'])
assert re.fullmatch('[A-Za-z0-9-]{1,48}', os.environ['FL_DEPLOY_TOKEN'])
assert 'REPLACE' not in os.environ['FL_DEPLOY_TOKEN']
assert os.environ['FL_CLOUD'] in ('aws', 'gcp')
assert os.environ['FL_DNS_PROVIDER'] in ('cloudflare', 'aws', 'gcp')
if os.environ['FL_DNS_PROVIDER'] == 'aws':
    assert os.environ['FL_CLOUD'] == 'aws', 'This Route 53 renewal branch uses an EC2 instance role'
if os.environ['FL_DNS_PROVIDER'] == 'cloudflare':
    assert re.fullmatch('[0-9a-f]{32}', os.environ['FL_CF_ZONE_ID'])
assert '://' not in os.environ['FL_DOMAIN'] and not os.environ['FL_DOMAIN'].endswith('.')
PYCHECK
git cat-file -e "$FL_REPO_REV^{commit}"
export FL_SSH_KEY="$FL_STATE/admin-ed25519"
test -f "$FL_SSH_KEY" || ssh-keygen -t ed25519 -f "$FL_SSH_KEY"
export FL_GATEWAY_SSH_PORT=${FL_GATEWAY_SSH_PORT:-22}
if [ -f "$FL_STATE/resources.env" ]; then
  source "$FL_STATE/resources.env"
fi
