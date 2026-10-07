# Deployment guide

This guide deploys the current repository from a checkout, with Linux services and macOS
agents. The commands target fresh hosts; edit the central settings before running them.
Use the same reviewed repository revision on all hosts. The renderer prepares files for review
and installation; it does not provision cloud resources or modify running services.

Follow sections 1–9 in order, choosing one cloud provisioning branch and one DNS provider.
The common `flssh`, `flput` and `flget` blocks execute the selected provider's transport commands. Sections 10/11
run on physical Macs and user machines for either cloud; section 12 is optional. Fill the
central settings once, preserve the protected operator state, and retain the resolved OS
image IDs to reproduce the same deployment inputs. Google web OAuth client registration,
Workspace group ownership, provider webhook approval and hardware mappings remain explicit
operator steps; this runbook supplies commands for everything the cloud CLIs can perform.

The default deployment has two Linux hosts and any number of Mac Minis. BWRC license
forwarding is optional and disabled for the current Vivado Lab setup. Kasa is a scaffold;
there is no smart-plug service or token to configure.

## Contents

1. [Components and prerequisites](#1-components-and-prerequisites)
2. [AWS or GCP hosting](#2-aws-or-gcp-hosting)
3. [Google login and Groups authorization](#3-google-login-and-groups-authorization)
4. [DNS, TLS and firewall configuration](#4-dns-tls-and-firewall-configuration)
5. [Linux checkout and service accounts](#5-linux-checkout-and-service-accounts)
6. [Headscale bootstrap and credentials](#6-headscale-bootstrap-and-credentials)
7. [Render and install deployment bundles](#7-render-and-install-deployment-bundles)
8. [Transfer gateway](#8-transfer-gateway)
9. [PostgreSQL, scheduler and dashboard](#9-postgresql-scheduler-and-dashboard)
10. [Mac agents, firmware and local interfaces](#10-mac-agents-firmware-and-local-interfaces)
11. [Remote client](#11-remote-client)
12. [Optional third-party integrations](#12-optional-third-party-integrations)
13. [Acceptance, maintenance and troubleshooting](#13-acceptance-maintenance-and-troubleshooting)

## 1. Components and prerequisites

| Component | Host | Account/configuration | Required |
| --- | --- | --- | --- |
| Headscale + nginx | Scheduler Linux host | `headscale`; `/etc/headscale` | Yes |
| PostgreSQL | Scheduler host or private database service | Dedicated database and login | Yes |
| Scheduler + React dashboard | Scheduler Linux host | `fl-scheduler`; `/etc/fl/scheduler.env` | Scheduler yes; UI optional |
| Gateway HTTP service + BBCP SSH service + nginx | Gateway Linux host | `fl-transfer`; `/etc/fl/transfer-gateway.env` | For remote artifact transfer |
| Tailscale infrastructure clients | Both Linux hosts and each Mac | Administrator-issued Headscale joins | Yes |
| Agent, workers, local SDK/CLI/TUI | Each Mac Mini | System launchd service | Yes |
| Remote client, BBCP and OpenSSH | User Linux/Chipyard or Mac host | User-owned client credentials | For terminal submission |
| Google OAuth + Workspace Groups | Google project and Workspace organization | OAuth client and group-owner service-account JSON (or delegated Directory) | Yes |
| Mailgun, Slack, Google Chat | External providers; workers run in scheduler | Optional protected notification JSON | No |
| BWRC license relay | Additional BWRC-connected Linux host | HAProxy and explicit private license ports | No |

You need domain/DNS control, Linux administrator access, Mac administrator access, and ownership
of the Workspace groups used for access. The group-owner option below does not require
Workspace domain-wide delegation or a super administrator to authorize a client ID, provided
your organization already allows the required Groups operations. The Google project is
needed even if the hosts run on AWS or on premises. AWS hosting is not required, and GCP
Compute Engine hosting is not required merely to use Google login.

Use Linux x86-64 and Apple Silicon macOS for the committed Pixi lock. Size storage for gateway
staging and Mac collateral retention, not just Python packages. PCB firmware is supplied by the
hardware team; production inventories must not use the mock backend.

On your workstation, verify the selected provider CLI and common tools before section 2:

```sh
# AWS:
aws --version
# GCP:
gcloud version
# Both:
jq --version
python3 --version
ssh -V
git --version
```

## 2. AWS or GCP hosting

### Command conventions and operator setup

Run cloud CLI, DNS, upload and `flssh` commands in **Bash on your operator workstation**.
Run blocks labelled **on the VM** after opening the indicated SSH session. Use a fresh
deployment; creation commands intentionally report an existing resource rather than silently
creating duplicates. Keep the same shell open, or reload the saved settings/resources below.
These commands provision billable infrastructure when you run them; this document does not
execute them. Install Google Cloud CLI for Google credential setup on either hosting provider, plus
AWS CLI v2 for AWS hosting, `jq`, Python 3 and OpenSSH locally.

Edit the following values once. `FL_CLOUD` selects VM hosting; `FL_DNS_PROVIDER` independently
selects authoritative DNS and certificate renewal. Keep an existing Cloudflare-managed domain
on Cloudflare: use `FL_DNS_PROVIDER=cloudflare` and its existing zone ID, without creating a
Cloud DNS/Route 53 zone or changing nameservers. `FL_DOMAIN` is your deployment subdomain,
such as `fl.example.edu`; service names become `scheduler.fl.example.edu`,
`headscale.fl.example.edu` and `transfer.fl.example.edu`. Supply a reviewed full Git commit
from your local checkout. Keep credentials outside the checkout.

```sh
set -euo pipefail
umask 077
export FL_STATE="$HOME/.local/state/fl-deploy"
mkdir -p "$FL_STATE"
chmod 700 "$FL_STATE"
cat > "$FL_STATE/settings.env" <<'ENV'
export FL_CLOUD=gcp                         # gcp or aws
export FL_DNS_PROVIDER=cloudflare            # cloudflare, gcp or aws; independent of VM hosting
export FL_DOMAIN=bringup.example.edu
export FL_CF_ZONE_ID=REPLACE_WITH_CLOUDFLARE_ZONE_ID  # Only for Cloudflare DNS; usually the parent domain's zone
export FL_VPC_CIDR=10.80.0.0/16
export FL_SUBNET_CIDR=10.80.0.0/24
export FL_SCHEDULER_NIC_IP=10.80.0.10
export FL_GATEWAY_NIC_IP=10.80.0.11
export FL_PIXI_VERSION=v0.65.0
export FL_ADMIN_CIDR=REPLACE_WITH_YOUR_PUBLIC_IPV4/32
export FL_REPO_REV=REPLACE_WITH_REVIEWED_FULL_COMMIT
export FL_DEPLOY_TOKEN=REPLACE_WITH_A_UNIQUE_DEPLOYMENT_NAME
export FL_GOOGLE_PROJECT=REPLACE_WITH_GOOGLE_PROJECT_ID
export FL_WORKSPACE_DOMAIN=example.edu
export FL_GCP_PROJECT=REPLACE_WITH_GCP_HOSTING_PROJECT_ID
export FL_GCP_REGION=us-west2
export FL_GCP_ZONE=us-west2-a
export FL_GCP_DNS_ZONE=REPLACE_WITH_EXISTING_MANAGED_ZONE_NAME  # Only for GCP DNS
export AWS_PROFILE=fl-admin
export AWS_DEFAULT_REGION=us-west-2
export FL_AWS_AZ=us-west-2a
export FL_AWS_ZONE_ID=REPLACE_WITH_EXISTING_ROUTE53_ZONE_ID  # Only for AWS DNS
ENV
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
export FL_GATEWAY_SSH_PORT=22
```

The key-generation prompt can protect your administrator key with a passphrase. Load it in
your SSH agent when needed. AWS hosting still needs a Google project for login/group access;
the hosting and Google projects may be separate. Select an AWS AZ supporting M7i, or change
the documented machine choices to available x86-64 equivalents.

Define these helpers once; they select real AWS/GCP connection commands. `flput`/`flget`
copy files or directories. Gateway management starts on port 22 and moves to 2222 in section 5.
Host keys use OpenSSH's `accept-new`: first contact records the key; changed keys are rejected.

```sh
cat > "$FL_STATE/helpers.sh" <<'SH'
fl_target() {
  case "$1" in
    scheduler) FL_TARGET_VM=fl-scheduler; FL_TARGET_IP=$FL_SCHEDULER_PUBLIC_IP; FL_TARGET_PORT=22 ;;
    gateway) FL_TARGET_VM=fl-transfer-gateway; FL_TARGET_IP=$FL_GATEWAY_PUBLIC_IP; FL_TARGET_PORT=$FL_GATEWAY_SSH_PORT ;;
    *) return 2 ;;
  esac
}
flssh() {
  fl_target "$1" || return; shift
  if [ "$FL_CLOUD" = gcp ]; then
    if [ "$#" -gt 0 ]; then
      gcloud compute ssh "ubuntu@$FL_TARGET_VM" --project="$FL_GCP_PROJECT" \
        --zone="$FL_GCP_ZONE" --ssh-key-file="$FL_SSH_KEY" \
        --ssh-flag="-p$FL_TARGET_PORT" --ssh-flag=-oStrictHostKeyChecking=accept-new --command="$1"
    else
      gcloud compute ssh "ubuntu@$FL_TARGET_VM" --project="$FL_GCP_PROJECT" \
        --zone="$FL_GCP_ZONE" --ssh-key-file="$FL_SSH_KEY" \
        --ssh-flag="-p$FL_TARGET_PORT" --ssh-flag=-oStrictHostKeyChecking=accept-new
    fi
  else
    ssh -i "$FL_SSH_KEY" -p "$FL_TARGET_PORT" -o StrictHostKeyChecking=accept-new \
      "ubuntu@$FL_TARGET_IP" "$@"
  fi
}
flput() {
  fl_target "$1" || return
  if [ "$FL_CLOUD" = gcp ]; then
    gcloud compute scp --recurse --project="$FL_GCP_PROJECT" --zone="$FL_GCP_ZONE" \
      --ssh-key-file="$FL_SSH_KEY" --port="$FL_TARGET_PORT" \
      --scp-flag=-oStrictHostKeyChecking=accept-new "$2" "ubuntu@$FL_TARGET_VM:$3"
  else
    scp -r -i "$FL_SSH_KEY" -P "$FL_TARGET_PORT" -o StrictHostKeyChecking=accept-new \
      "$2" "ubuntu@$FL_TARGET_IP:$3"
  fi
}
flget() {
  fl_target "$1" || return
  if [ "$FL_CLOUD" = gcp ]; then
    gcloud compute scp --recurse --project="$FL_GCP_PROJECT" --zone="$FL_GCP_ZONE" \
      --ssh-key-file="$FL_SSH_KEY" --port="$FL_TARGET_PORT" \
      --scp-flag=-oStrictHostKeyChecking=accept-new "ubuntu@$FL_TARGET_VM:$2" "$3"
  else
    scp -r -i "$FL_SSH_KEY" -P "$FL_TARGET_PORT" -o StrictHostKeyChecking=accept-new \
      "ubuntu@$FL_TARGET_IP:$2" "$3"
  fi
}
flsave() {
  local name
  : > "$FL_STATE/resources.env"
  for name in FL_CLOUD FL_SCHEDULER_PUBLIC_IP FL_GATEWAY_PUBLIC_IP FL_SCHEDULER_NIC_IP \
    FL_GATEWAY_NIC_IP FL_GATEWAY_SSH_PORT FL_AWS_VPC FL_AWS_SUBNET FL_AWS_IGW \
    FL_AWS_RT FL_AWS_SCHEDULER_SG FL_AWS_GATEWAY_SG FL_AWS_SCHEDULER_ID \
    FL_AWS_GATEWAY_ID FL_AWS_SCHEDULER_EIP FL_AWS_GATEWAY_EIP \
    FL_AWS_SCHEDULER_VOLUME FL_AWS_GATEWAY_VOLUME FL_AWS_AMI \
    FL_SCHEDULER_HEADSCALE_IP FL_GATEWAY_HEADSCALE_IP; do
    if [ -n "${!name:-}" ]; then
      printf 'export %s=%q\n' "$name" "${!name}" >> "$FL_STATE/resources.env"
    fi
  done
}
SH
source "$FL_STATE/helpers.sh"
```

The helper block is saved as `$FL_STATE/helpers.sh` for resuming in another shell.
Reload with `source "$FL_STATE/settings.env"`, `source "$FL_STATE/resources.env"` and
`source "$FL_STATE/helpers.sh"`; reset `FL_STATE` and `FL_SSH_KEY` as above first.
References: [gcloud SSH](https://docs.cloud.google.com/sdk/gcloud/reference/compute/ssh),
[gcloud SCP](https://docs.cloud.google.com/sdk/gcloud/reference/compute/scp).

Choose one hosting branch, or use existing Linux hosts. Cloud credentials manage infrastructure,
DNS, backups or secret retrieval; the scheduler itself does not call AWS APIs or automatically
fetch AWS/GCP secrets. Materialize its configuration as the files/environment described below.

### AWS

Use an organization-approved AWS account and region. Authenticate the deployment operator
with an existing IAM Identity Center permission set:

```sh
aws configure sso --profile "$AWS_PROFILE"
aws sso login --profile "$AWS_PROFILE"
aws sts get-caller-identity --profile "$AWS_PROFILE"
```

From the operator shell, provision the AWS equivalent sizes: **`m7i.large` (2 vCPU/8 GiB)**
for the scheduler and **`m7i.xlarge` (4 vCPU/16 GiB)** for the gateway, with 50 GiB gp3 boot
disks and 100/500 GiB gp3 data disks. These are on-demand x86-64 instances. The new VPC uses
one public subnet and an internet-gateway route; Elastic IPs provide stable public addresses.
Only administrator-source SSH is enabled initially; section 8 opens transfer ports later.

```sh
export FL_CLOUD=aws
export FL_AWS_ZONE_ID="${FL_AWS_ZONE_ID##*/}"
aws sts get-caller-identity
aws ec2 describe-instance-type-offerings --location-type availability-zone \
  --filters Name=instance-type,Values=m7i.large,m7i.xlarge \
  --query 'InstanceTypeOfferings[].{Type:InstanceType,AZ:Location}' --output table
FL_AWS_VPC=$(aws ec2 create-vpc --cidr-block "$FL_VPC_CIDR" \
  --tag-specifications 'ResourceType=vpc,Tags=[{Key=Name,Value=fl-vpc}]' \
  --query Vpc.VpcId --output text)
export FL_AWS_VPC
aws ec2 modify-vpc-attribute --vpc-id "$FL_AWS_VPC" --enable-dns-support Value=true
aws ec2 modify-vpc-attribute --vpc-id "$FL_AWS_VPC" --enable-dns-hostnames Value=true
FL_AWS_SUBNET=$(aws ec2 create-subnet --vpc-id "$FL_AWS_VPC" \
  --cidr-block "$FL_SUBNET_CIDR" --availability-zone "$FL_AWS_AZ" \
  --tag-specifications 'ResourceType=subnet,Tags=[{Key=Name,Value=fl-subnet}]' \
  --query Subnet.SubnetId --output text)
export FL_AWS_SUBNET
FL_AWS_IGW=$(aws ec2 create-internet-gateway \
  --query InternetGateway.InternetGatewayId --output text)
export FL_AWS_IGW
aws ec2 attach-internet-gateway --vpc-id "$FL_AWS_VPC" --internet-gateway-id "$FL_AWS_IGW"
FL_AWS_RT=$(aws ec2 create-route-table --vpc-id "$FL_AWS_VPC" \
  --query RouteTable.RouteTableId --output text)
export FL_AWS_RT
aws ec2 create-route --route-table-id "$FL_AWS_RT" --destination-cidr-block 0.0.0.0/0 \
  --gateway-id "$FL_AWS_IGW"
aws ec2 associate-route-table --route-table-id "$FL_AWS_RT" --subnet-id "$FL_AWS_SUBNET"
FL_AWS_SCHEDULER_SG=$(aws ec2 create-security-group --vpc-id "$FL_AWS_VPC" \
  --group-name fl-scheduler --description 'Fletcherlake scheduler' --query GroupId --output text)
export FL_AWS_SCHEDULER_SG
FL_AWS_GATEWAY_SG=$(aws ec2 create-security-group --vpc-id "$FL_AWS_VPC" \
  --group-name fl-transfer-gateway --description 'Fletcherlake gateway' --query GroupId --output text)
export FL_AWS_GATEWAY_SG
for FL_SG in "$FL_AWS_SCHEDULER_SG" "$FL_AWS_GATEWAY_SG"; do
  aws ec2 authorize-security-group-ingress --group-id "$FL_SG" \
    --protocol tcp --port 443 --cidr 0.0.0.0/0
  aws ec2 authorize-security-group-ingress --group-id "$FL_SG" \
    --protocol udp --port 41641 --cidr 0.0.0.0/0
  aws ec2 authorize-security-group-ingress --group-id "$FL_SG" \
    --protocol tcp --port 22 --cidr "$FL_ADMIN_CIDR"
done
aws ec2 authorize-security-group-ingress --group-id "$FL_AWS_GATEWAY_SG" \
  --protocol tcp --port 2222 --cidr "$FL_ADMIN_CIDR"
aws ec2 import-key-pair --key-name fl-admin --public-key-material "fileb://$FL_SSH_KEY.pub"
flsave
```

Run the following block for every AWS deployment; it always requires IMDSv2 and adds a
DNS-only VM instance role only for `FL_DNS_PROVIDER=aws`. That role permits unattended
Route 53 certificate renewal and can change only TXT challenge records under `FL_DOMAIN`
in your existing hosted zone. Operator DNS commands use your separate AWS profile.

```sh
FL_AWS_PROFILE_ARGS=(--metadata-options HttpTokens=required)
if [ "$FL_DNS_PROVIDER" = aws ]; then
cat > "$FL_STATE/ec2-trust.json" <<'JSON'
{"Version":"2012-10-17","Statement":[{"Effect":"Allow","Principal":{"Service":"ec2.amazonaws.com"},"Action":"sts:AssumeRole"}]}
JSON
jq -n --arg zone "$FL_AWS_ZONE_ID" --arg pattern "_acme-challenge.*.$FL_DOMAIN" \
  '{Version:"2012-10-17",Statement:[
    {Effect:"Allow",Action:["route53:ListHostedZones"],Resource:"*"},
    {Effect:"Allow",Action:["route53:GetChange"],Resource:"arn:aws:route53:::change/*"},
    {Effect:"Allow",Action:["route53:ChangeResourceRecordSets"],
     Resource:("arn:aws:route53:::hostedzone/"+$zone),Condition:{
       "ForAllValues:StringLike":{"route53:ChangeResourceRecordSetsNormalizedRecordNames":[$pattern]},
       "ForAllValues:StringEquals":{"route53:ChangeResourceRecordSetsRecordTypes":["TXT"]}}}
  ]}' > "$FL_STATE/certbot-policy.json"
aws iam create-role --role-name fl-certbot \
  --assume-role-policy-document "file://$FL_STATE/ec2-trust.json"
aws iam put-role-policy --role-name fl-certbot --policy-name fl-certbot-dns \
  --policy-document "file://$FL_STATE/certbot-policy.json"
aws iam create-instance-profile --instance-profile-name fl-certbot
aws iam add-role-to-instance-profile --instance-profile-name fl-certbot --role-name fl-certbot
FL_AWS_PROFILE_ARGS+=(--iam-instance-profile Name=fl-certbot)
fi
```

Resolve and save the current Canonical Ubuntu 24.04 AMI before launching. Keep the saved AMI
ID to repeat this exact image later; `current` changes as Canonical publishes updates. If the
new instance profile has not propagated yet, wait and retry the launch with the same client token.

```sh
FL_AWS_AMI=${FL_AWS_AMI:-$(aws ssm get-parameter \
  --name /aws/service/canonical/ubuntu/server/24.04/stable/current/amd64/hvm/ebs-gp3/ami-id \
  --query Parameter.Value --output text)}
export FL_AWS_AMI
FL_ROOT_DEVICE=$(aws ec2 describe-images --image-ids "$FL_AWS_AMI" \
  --query 'Images[0].RootDeviceName' --output text)
for FL_ROLE in scheduler gateway; do
  if [ "$FL_ROLE" = scheduler ]; then
    FL_VM=fl-scheduler; FL_TYPE=m7i.large; FL_SIZE=100; FL_IP=$FL_SCHEDULER_NIC_IP; FL_SG=$FL_AWS_SCHEDULER_SG
  else
    FL_VM=fl-transfer-gateway; FL_TYPE=m7i.xlarge; FL_SIZE=500; FL_IP=$FL_GATEWAY_NIC_IP; FL_SG=$FL_AWS_GATEWAY_SG
  fi
  jq -n --arg root "$FL_ROOT_DEVICE" --argjson size "$FL_SIZE" \
    '[{DeviceName:$root,Ebs:{VolumeSize:50,VolumeType:"gp3",Encrypted:true,DeleteOnTermination:true}},
      {DeviceName:"/dev/sdf",Ebs:{VolumeSize:$size,VolumeType:"gp3",Encrypted:true,DeleteOnTermination:false}}]' \
    > "$FL_STATE/$FL_ROLE-disks.json"
  aws ec2 run-instances --image-id "$FL_AWS_AMI" --instance-type "$FL_TYPE" --count 1 \
    --client-token "$FL_DEPLOY_TOKEN-$FL_ROLE" --key-name fl-admin \
    --subnet-id "$FL_AWS_SUBNET" --private-ip-address "$FL_IP" --security-group-ids "$FL_SG" \
    "${FL_AWS_PROFILE_ARGS[@]}" \
    --block-device-mappings "file://$FL_STATE/$FL_ROLE-disks.json" \
    --tag-specifications "ResourceType=instance,Tags=[{Key=Name,Value=$FL_VM}]" \
    > "$FL_STATE/$FL_ROLE-instance.json"
done
FL_AWS_SCHEDULER_ID=$(jq -r '.Instances[0].InstanceId' "$FL_STATE/scheduler-instance.json")
export FL_AWS_SCHEDULER_ID
FL_AWS_GATEWAY_ID=$(jq -r '.Instances[0].InstanceId' "$FL_STATE/gateway-instance.json")
export FL_AWS_GATEWAY_ID
for FL_ROLE in scheduler gateway; do
  aws ec2 allocate-address --domain vpc > "$FL_STATE/$FL_ROLE-eip.json"
  FL_ID=$(jq -r '.Instances[0].InstanceId' "$FL_STATE/$FL_ROLE-instance.json")
  FL_ALLOCATION=$(jq -r '.AllocationId' "$FL_STATE/$FL_ROLE-eip.json")
  aws ec2 associate-address --instance-id "$FL_ID" --allocation-id "$FL_ALLOCATION"
done
FL_AWS_SCHEDULER_EIP=$(jq -r .AllocationId "$FL_STATE/scheduler-eip.json")
export FL_AWS_SCHEDULER_EIP
FL_AWS_GATEWAY_EIP=$(jq -r .AllocationId "$FL_STATE/gateway-eip.json")
export FL_AWS_GATEWAY_EIP
FL_SCHEDULER_PUBLIC_IP=$(jq -r .PublicIp "$FL_STATE/scheduler-eip.json")
export FL_SCHEDULER_PUBLIC_IP
FL_GATEWAY_PUBLIC_IP=$(jq -r .PublicIp "$FL_STATE/gateway-eip.json")
export FL_GATEWAY_PUBLIC_IP
aws ec2 wait instance-status-ok --instance-ids "$FL_AWS_SCHEDULER_ID" "$FL_AWS_GATEWAY_ID"
FL_AWS_SCHEDULER_VOLUME=$(aws ec2 describe-instances --instance-ids "$FL_AWS_SCHEDULER_ID" \
  --query "Reservations[0].Instances[0].BlockDeviceMappings[?DeviceName=='/dev/sdf'].Ebs.VolumeId | [0]" --output text)
export FL_AWS_SCHEDULER_VOLUME
FL_AWS_GATEWAY_VOLUME=$(aws ec2 describe-instances --instance-ids "$FL_AWS_GATEWAY_ID" \
  --query "Reservations[0].Instances[0].BlockDeviceMappings[?DeviceName=='/dev/sdf'].Ebs.VolumeId | [0]" --output text)
export FL_AWS_GATEWAY_VOLUME
flsave
flssh scheduler 'hostname; ip -4 address'
flssh gateway 'hostname; ip -4 address'
```

Sources: [Canonical AMI discovery](https://ubuntu.com/aws/docs/aws-how-to/instances/find-ubuntu-images/),
[EC2 launch commands](https://docs.aws.amazon.com/cli/latest/reference/ec2/run-instances.html),
[M7i specifications](https://aws.amazon.com/ec2/instance-types/m7i/),
[Route 53 renewal permissions](https://certbot-dns-route53.readthedocs.io/en/stable/).

Provision two Linux x86-64 EC2 instances, persistent disks, public addresses and security groups.
Use reserved public addresses if DNS must stay stable. On NAT-backed instances, the manifest's
`public_ip` is the VM's actual local interface address; public DNS points to the externally
mapped address. Reserve a separate administrative SSH port/address for the gateway.

If using Route 53 or Secrets Manager from a VM, attach an EC2 IAM role/instance profile with
only the needed permissions. Do not put AWS access keys in the scheduler environment. For
secret retrieval, scope `secretsmanager:GetSecretValue` to the selected secret ARNs; add
`kms:Decrypt` on the relevant customer-managed key only if required. An operator or deployment
agent must write retrieved values into protected local files before service startup.
See [AWS CLI authentication](https://docs.aws.amazon.com/cli/latest/userguide/cli-chap-authentication.html),
[EC2 instance profiles](https://docs.aws.amazon.com/IAM/latest/UserGuide/id_roles_use_switch-role-ec2_instance-profiles.html),
and [Secrets Manager policies](https://docs.aws.amazon.com/secretsmanager/latest/userguide/auth-and-access_iam-policies.html).

### GCP

For Compute Engine hosting, select a project with billing and enable Compute Engine. Enable
Cloud DNS or Secret Manager only if using them:

```sh
gcloud auth login
gcloud config set project "$FL_GCP_PROJECT"
gcloud services enable compute.googleapis.com
# Optional integrations:
gcloud services enable dns.googleapis.com secretmanager.googleapis.com
```

Provision two Linux x86-64 VMs, persistent disks, reserved external addresses and targeted VPC
firewall rules. The manifest again uses actual NIC addresses for public-facing bind addresses.

For an initial lab deployment with modest job/transfer concurrency, use these starting sizes.
These are sizing recommendations, not measured production capacity guarantees:

| VM | Machine type | vCPUs / memory | Boot disk | Separate data disk |
| --- | --- | --- | --- | --- |
| `fl-scheduler`: scheduler, PostgreSQL, dashboard, Headscale and nginx | `e2-standard-2` | 2 / 8 GB | 50 GiB `pd-balanced` | 100 GiB `pd-balanced` for PostgreSQL and service state |
| `fl-transfer-gateway`: BBCP/OpenSSH, Tailscale and nginx | `e2-standard-4` | 4 / 16 GB | 50 GiB `pd-balanced` | Initially 500 GiB `pd-balanced` for `/var/lib/fl-transfer` |

Use Ubuntu 24.04 LTS x86-64, regular non-Spot VMs, and the same region near the physical Macs.
Mount data disks before creating service state or starting services. Ensure mounts are present
on subsequent boots before their services start. Back up PostgreSQL independently of gateway
staging; increasing disk size or VM resources does not provide service redundancy.

The scheduler coordinates work; execution happens on the Macs. The gateway needs more CPU
headroom for encrypted transfers and SHA verification, and more storage for the actual payloads.
Its input files remain until one day after the original upload grant, including after a Mac
downloads them. Exported results have their own retention deadlines. Size its data disk for
**all uploads in that one-day window + all retained exports + at least 30% free space**;
500 GiB is only a starting point. Monitor disk usage, CPU and actual transfer throughput.

Google lists `e2-standard-2` and `e2-standard-4` with maximum egress bandwidth of up to 4 and
8 Gbps respectively. These are VM ceilings, not BBCP speed guarantees: disk performance,
encryption, shared uplinks and the Mac's network can limit transfers first. Balanced disk
throughput also depends on provisioned capacity and VM limits. For sustained transfer needs,
measure the bottleneck before changing the gateway's machine type or disk.
See [E2 machine specifications](https://docs.cloud.google.com/compute/docs/general-purpose-machines#e2_machine_types)
and [Persistent Disk performance](https://docs.cloud.google.com/compute/docs/disks/performance).

#### GCP NIC and VM creation settings

In Compute Engine's VM creation form, use the machine/disk sizes above and these settings.
Create a custom-mode VPC named `fl-vpc` and a regional subnet named `fl-subnet` first. Choose
an unused subnet CIDR, for example `10.80.0.0/24`, that does not overlap campus/Mac networks
or Headscale's `100.64.0.0/10`. Both VMs use that subnet and the same selected region.

The `10.80.0.10` / `10.80.0.11` examples work only in that custom subnet. Selecting the
regional subnet named `default` and entering either address produces **Requested IP is not
within the range of subnetwork 'default'**. To fix this in the console:

1. Open **VPC networks → Create VPC network**.
2. Name it `fl-vpc` and choose **Custom** subnet creation.
3. Add `fl-subnet` in the same region as your VMs, with IPv4 range `10.80.0.0/24`.
4. In the internal-IP reservation form, select `fl-subnet`. Reserve `10.80.0.10` for the
   scheduler and `10.80.0.11` for the gateway.
5. In each VM's **Network interfaces → nic0** settings, select **Network: fl-vpc →
   Subnetwork: fl-subnet** and its corresponding reserved internal address. Retry VM creation
   with these selections; selecting `default` again will produce the same error.

If `fl-vpc` and `fl-subnet` already exist, use them rather than recreating them.

Equivalent commands for a fresh deployment, using the project selected above (replace the
region before running; use the existing resources if already created):

```sh
gcloud compute networks create fl-vpc --subnet-mode=custom
gcloud compute networks subnets create fl-subnet \
  --network=fl-vpc --region="$FL_GCP_REGION" --range="$FL_SUBNET_CIDR"
gcloud compute addresses create fl-scheduler-internal \
  --region="$FL_GCP_REGION" --subnet=fl-subnet --addresses="$FL_SCHEDULER_NIC_IP"
gcloud compute addresses create fl-gateway-internal \
  --region="$FL_GCP_REGION" --subnet=fl-subnet --addresses="$FL_GATEWAY_NIC_IP"
```

Continue on the operator workstation. This creates all bootstrap firewall rules, reserved
external addresses and both VMs. The Cloud DNS renewal identity is created separately in
section 4; these VMs use no attached Google service account. The scheduler's group-reading
JSON key is a separate identity configured in section 3.

```sh
export FL_CLOUD=gcp
gcloud config set project "$FL_GCP_PROJECT"
gcloud services enable compute.googleapis.com
if [ "$FL_DNS_PROVIDER" = gcp ]; then gcloud services enable dns.googleapis.com; fi
gcloud compute firewall-rules create fl-public-https --network=fl-vpc \
  --direction=INGRESS --priority=1000 --action=ALLOW --rules=tcp:443 \
  --source-ranges=0.0.0.0/0 --target-tags=fl-scheduler,fl-transfer-gateway
gcloud compute firewall-rules create fl-tailnet-direct --network=fl-vpc \
  --direction=INGRESS --priority=1000 --action=ALLOW --rules=udp:41641 \
  --source-ranges=0.0.0.0/0 --target-tags=fl-scheduler,fl-transfer-gateway
gcloud compute firewall-rules create fl-bootstrap-admin --network=fl-vpc \
  --direction=INGRESS --priority=1000 --action=ALLOW --rules=tcp:22 \
  --source-ranges="$FL_ADMIN_CIDR" --target-tags=fl-scheduler,fl-transfer-gateway
gcloud compute firewall-rules create fl-gateway-admin --network=fl-vpc \
  --direction=INGRESS --priority=1000 --action=ALLOW --rules=tcp:2222 \
  --source-ranges="$FL_ADMIN_CIDR" --target-tags=fl-transfer-gateway
gcloud compute addresses create fl-scheduler-public fl-gateway-public \
  --region="$FL_GCP_REGION" --network-tier=PREMIUM
FL_SCHEDULER_PUBLIC_IP=$(gcloud compute addresses describe fl-scheduler-public \
  --region="$FL_GCP_REGION" --format='value(address)')
export FL_SCHEDULER_PUBLIC_IP
FL_GATEWAY_PUBLIC_IP=$(gcloud compute addresses describe fl-gateway-public \
  --region="$FL_GCP_REGION" --format='value(address)')
export FL_GATEWAY_PUBLIC_IP
if [ ! -f "$FL_STATE/gcp-image.json" ]; then
  gcloud compute images describe-from-family ubuntu-2404-lts-amd64 \
    --project=ubuntu-os-cloud --format=json > "$FL_STATE/gcp-image.json"
fi
FL_GCP_IMAGE=$(jq -r .selfLink "$FL_STATE/gcp-image.json")
for FL_ROLE in scheduler gateway; do
  if [ "$FL_ROLE" = scheduler ]; then
    FL_VM=fl-scheduler; FL_TYPE=e2-standard-2; FL_SIZE=100
    FL_IP=$FL_SCHEDULER_NIC_IP; FL_EXTERNAL=$FL_SCHEDULER_PUBLIC_IP
  else
    FL_VM=fl-transfer-gateway; FL_TYPE=e2-standard-4; FL_SIZE=500
    FL_IP=$FL_GATEWAY_NIC_IP; FL_EXTERNAL=$FL_GATEWAY_PUBLIC_IP
  fi
  printf 'ubuntu:%s\n' "$(cat "$FL_SSH_KEY.pub")" > "$FL_STATE/gcp-ssh-keys"
  gcloud compute instances create "$FL_VM" --project="$FL_GCP_PROJECT" \
    --zone="$FL_GCP_ZONE" --machine-type="$FL_TYPE" --provisioning-model=STANDARD \
    --subnet=fl-subnet --private-network-ip="$FL_IP" --address="$FL_EXTERNAL" \
    --network-tier=PREMIUM --stack-type=IPV4_ONLY --tags="$FL_VM" \
    --image="$FL_GCP_IMAGE" --boot-disk-size=50GB --boot-disk-type=pd-balanced \
    --create-disk="name=$FL_VM-data,device-name=fl-data,size=${FL_SIZE}GB,type=pd-balanced,auto-delete=no" \
    --no-service-account --no-scopes \
    --metadata-from-file="ssh-keys=$FL_STATE/gcp-ssh-keys" --metadata=block-project-ssh-keys=TRUE
done
flsave
flssh scheduler 'hostname; ip -4 address'
flssh gateway 'hostname; ip -4 address'
```

The selected image's exact self-link is saved in `gcp-image.json`; reuse it rather than
resolving the family again to repeat the same OS image. If your organization requires OS Login,
use its approved OS Login identity and adjust the helper username; do not disable organization
policy. The example `ubuntu` metadata-key setup assumes metadata-based SSH is permitted.
References: [VM creation flags](https://docs.cloud.google.com/sdk/gcloud/reference/compute/instances/create)
and [Ubuntu images](https://docs.cloud.google.com/compute/docs/images/os-details).

Alternatively, to keep the `default` subnet, select **Automatic** for the VM's internal IPv4
address rather than entering `10.80.0.11`. For a stable internal address, reserve an available
address within that subnet's actual CIDR or promote the assigned address to static. Inspect
the subnet's range with:

```sh
gcloud compute networks subnets describe default \
  --region="$FL_GCP_REGION" --format='value(ipCidrRange)'
```

Select automatic internal-IP allocation or reserve an available address within that returned
range. Use the resulting real NIC addresses in the deployment manifest and put all firewall
rules on the VPC actually selected by the VMs. The dedicated `fl-vpc` is the plan's default;
if reusing the GCP `default` network, review its existing broad SSH/internal firewall rules
against the per-role rules below. See [subnet creation](https://docs.cloud.google.com/sdk/gcloud/reference/compute/networks/subnets/create)
and [internal address reservation](https://docs.cloud.google.com/sdk/gcloud/reference/compute/addresses/create).

| Setting | `fl-scheduler` | `fl-transfer-gateway` |
| --- | --- | --- |
| OS image / architecture | Ubuntu 24.04 LTS / x86-64 | Ubuntu 24.04 LTS / x86-64 |
| Provisioning model | Standard (non-Spot) | Standard (non-Spot) |
| GCP network interfaces | One: `nic0` | One: `nic0` |
| VPC / subnet | `fl-vpc` / `fl-subnet` | `fl-vpc` / `fl-subnet` |
| NIC type | VirtIO-Net (E2 default) | VirtIO-Net (E2 default) |
| IP stack | IPv4 only | IPv4 only |
| Primary internal IPv4 | Reserve `10.80.0.10` in the example subnet | Reserve `10.80.0.11` in the example subnet |
| External IPv4 | Separate reserved regional static address | Separate reserved regional static address |
| Network service tier | Premium; match the external address's tier | Premium; match the external address's tier |
| Network tag | `fl-scheduler` | `fl-transfer-gateway` |
| IP forwarding / alias IP ranges | Off / none | Off / none |
| Allow HTTP traffic checkbox | Unchecked | Unchecked |
| Allow HTTPS traffic checkbox | Unchecked when using the explicit rules below | Unchecked when using the explicit rules below |

**HTTPS must still be allowed on both VMs.** The table uses explicitly targeted firewall rules
instead of the convenience checkboxes. If you use the console's **Allow HTTPS traffic**
checkbox instead, verify that it installs a TCP-443 allow rule on this VPC for these VMs.
Leave **Allow HTTP traffic** unchecked: the supplied nginx configuration has no port-80
listener, and certificate issuance/renewal uses DNS-01. If you later want HTTP redirects,
add both a deliberate nginx port-80 redirect and its firewall rule.

Tailscale creates `tailscale0` inside Linux after joining Headscale. Its allocated `100.64.x.x`
address is not another GCP NIC or a GCP subnet address. Keep the subnet/exit-route settings
disabled as described in section 6. The static external IPv4 on each VM provides internet
connectivity; nginx terminates TLS directly on the host.

GCP maps an external IPv4 to the NIC's primary internal IPv4. Use the **internal NIC address**
for this repository's `public_ip` listener fields, and the actual Headscale address for
`private_ip`. For the example subnet:

| Address use | Scheduler | Gateway |
| --- | --- | --- |
| Deployment YAML `public_ip` (local public-facing bind) | `10.80.0.10` | `10.80.0.11` |
| Deployment YAML `private_ip` | Actual `tailscale ip -4` result | Actual `tailscale ip -4` result |
| Public DNS A records | `scheduler` and `headscale` names → scheduler's reserved external IPv4 | `transfer` name → gateway's reserved external IPv4 |
| Headscale private DNS | Agent name → scheduler's Headscale IPv4 | Internal gateway name → gateway's Headscale IPv4 |

The name `public_ip` describes the listener's purpose; do not try to bind nginx or SSH to an
external IPv4 that is absent from the guest's interfaces. Section 7's renderer installs these
local binds while the public gateway endpoint uses its public DNS hostname.
See [GCP IP addresses](https://docs.cloud.google.com/compute/docs/ip-addresses),
[external IPv4 NAT behavior](https://docs.cloud.google.com/nat/docs/public-nat),
and [NIC defaults](https://docs.cloud.google.com/compute/docs/networking/network-overview).

#### GCP firewall rules

Create these **ingress Allow** VPC firewall rules at priority `1000`, targeted by the VM network
tags above. Use `0.0.0.0/0` only for the explicitly public services. `ADMIN_EGRESS_CIDR`
means your actual administrator/VPN public source range, usually a single IPv4 `/32`.

| Rule | Target tag(s) | Allowed protocol/ports | Source IPv4 ranges | Purpose |
| --- | --- | --- | --- | --- |
| `fl-public-https` | `fl-scheduler`, `fl-transfer-gateway` | TCP `443` | `0.0.0.0/0` | Scheduler/login, Headscale coordination and gateway verification |
| `fl-gateway-transfer` | `fl-transfer-gateway` | TCP `22`, `5000-5099` | `0.0.0.0/0` | Scoped BBCP SSH/bootstrap and payloads from arbitrary Chipyard hosts |
| `fl-tailnet-direct` | `fl-scheduler`, `fl-transfer-gateway` | UDP `41641` | `0.0.0.0/0` | Recommended for direct encrypted Tailscale peer connections; confirm tailscaled's actual port |
| `fl-scheduler-admin` | `fl-scheduler` | TCP `22` | `ADMIN_EGRESS_CIDR` | Administrative SSH |
| `fl-gateway-admin` | `fl-transfer-gateway` | TCP `2222` | `ADMIN_EGRESS_CIDR` | Administrative SSH after moving the OS daemon off transfer port 22 |

On a fresh gateway, initially permit administrative TCP 22 **only from `ADMIN_EGRESS_CIDR`**.
Move the OS SSH daemon to port 2222 and verify a second login there before enabling the public
`fl-gateway-transfer` rule or starting the dedicated transfer SSH daemon. Check distro SSH
socket units as well as `sshd_config` so the OS daemon actually releases port 22. Port 2222
is a management choice for this plan; transfer metadata continues to use port 22. The
console's default SSH connection targets port 22; use an SSH client configured for 2222 to
administer the completed gateway. Keep the initial session open until the new path works.

Section 8 contains the command that opens the gateway's final public transfer rule after
its dedicated SSH daemon is installed. Do not open that rule during initial administrative
SSH bootstrap.

Adjust the BBCP range if the manifest changes. The custom VPC keeps implied ingress denial
for other ports; ensure inherited organization policies and any additional project rules
agree. In particular, do not add a general all-ports internal rule or a world-accessible
administrative SSH rule. PostgreSQL `5432`, application `8080/8081`, and Headscale metrics/gRPC
`9091/50443` stay off the VPC ingress allow list and bind to loopback where configured.

Keep the normal implied outbound allow policy for initial deployment. Outbound traffic needs
DNS, Google APIs and package/certificate/provider endpoints; Tailscale also needs outbound
HTTPS to external DERP servers, UDP 3478 for STUN and UDP to peers' advertised ports, which
can vary with NAT. If organization policy restricts egress, allow those dependencies explicitly.
The shipped Headscale configuration has its embedded DERP server disabled, so it needs no
public inbound STUN/3478 listener.

Mirror the required listeners in any host firewall. GCP sees the encrypted Tailscale transport;
inner connections to the `100.64.x.x` services are controlled by Headscale ACLs and the Linux
host firewall on `tailscale0`. Allow private scheduler HTTPS and private gateway HTTPS/SSH/BBCP
according to the rendered ACLs. Verify direct and DERP fallback paths before production file
transfers; the VPC TCP-443 rule alone does not establish private agent connectivity.
See [GCP firewall rules](https://docs.cloud.google.com/firewall/docs/firewalls),
[rule creation flags](https://docs.cloud.google.com/sdk/gcloud/reference/compute/firewall-rules/create),
and [Tailscale firewall ports](https://tailscale.com/docs/reference/faq/firewall-ports).

#### GCP runtime credentials

Attach a dedicated runtime service account if a VM must access cloud resources. Grant
`roles/secretmanager.secretAccessor` on individual required secrets; configure suitable VM
access scopes as well. The deployment process must retrieve and install those files.

`gcloud auth application-default login` is for local tools that use Application Default
Credentials (ADC), not a prerequisite for the scheduler. Its Google Groups adapters
currently require a service-account JSON key and do not substitute VM ADC credentials.
Keep that group-reading identity separate from DNS/secret-management identities.
See [Compute Engine workload authentication](https://docs.cloud.google.com/compute/docs/access/app-authentication-methods),
[Secret Manager authentication](https://docs.cloud.google.com/secret-manager/docs/authentication),
and [secret access roles](https://docs.cloud.google.com/secret-manager/docs/access-control).

## 3. Google login and Groups authorization

There are two distinct credentials: a web OAuth client for people and a service
account for group membership checks. A generic Google API key cannot replace either.

**AWS and GCP use the same Google commands here.** On the operator workstation, authenticate
to the Google project, create the dedicated group-reader identity and store its JSON key:

```sh
gcloud auth login
gcloud services enable cloudidentity.googleapis.com --project="$FL_GOOGLE_PROJECT"
gcloud iam service-accounts create fl-group-reader --project="$FL_GOOGLE_PROJECT" \
  --display-name='Fletcherlake group reader'
gcloud iam service-accounts keys create "$FL_STATE/google-groups.json" \
  --project="$FL_GOOGLE_PROJECT" \
  --iam-account="fl-group-reader@$FL_GOOGLE_PROJECT.iam.gserviceaccount.com"
chmod 600 "$FL_STATE/google-groups.json"
```

Complete the OAuth web-client creation and group-owner steps below in Google's console.
Google does not provide a general `gcloud` command to create this web OAuth client; an AWS
command cannot replace that step either. The service-account commands do not grant Workspace
group ownership. Add `fl-group-reader@...` directly as an Owner of each configured group,
and add the initial human administrator directly to the administrator group.

Then collect the two web-client values without placing the secret in command arguments:

```sh
read -r -p 'Google web OAuth client ID: ' FL_INPUT
printf '%s\n' "$FL_INPUT" > "$FL_STATE/google-client-id"
read -r -s -p 'Google web OAuth client secret: ' FL_INPUT
printf '\n'
printf '%s\n' "$FL_INPUT" > "$FL_STATE/google-client-secret"
unset FL_INPUT
```

Section 9 uploads these files through the selected AWS/GCP SSH helper and generates the
protected scheduler environment. No browser/client or Mac receives these credentials.

### OAuth web client

1. Select/create the Google Cloud project used for this application.
2. In **Google Auth platform → Branding**, configure the app name, support email and contacts.
   Select an Internal audience if every user is in your Workspace; otherwise configure the
   External audience and its test/publishing requirements.
3. Create a **Web application** client under **Google Auth platform → Clients**.
4. Register the exact redirect URI printed by `printf 'https://scheduler.%s/api/auth/callback\n' "$FL_DOMAIN"`. No localhost terminal callback or television/device OAuth client is needed.
5. Save the client ID and secret as `FL_GOOGLE_CLIENT_ID` and `FL_GOOGLE_CLIENT_SECRET`.

The current scheduler requests `openid email`. Group-reading scopes belong to the service account,
not this browser consent flow. The React application never receives the OAuth client secret.
See Google's [consent setup](https://developers.google.com/workspace/guides/configure-oauth-consent)
and [credential creation](https://developers.google.com/workspace/guides/create-credentials).

### Group-owner service account (no domain-wide delegation)

Use this option if you can create Workspace groups and make yourself their owner. Google
documents that a service account can be an **owner of specific groups** without receiving the
domain-wide Group Administrator role. This is separate from granting IAM roles in GCP.

1. Ensure these are Workspace **Groups for Business**, with end-user group creation already
   allowed. Consumer `@googlegroups.com` groups are not supported by this API.
2. Enable **Cloud Identity API** (`cloudidentity.googleapis.com`) in your Google project:

   ```sh
   gcloud services enable cloudidentity.googleapis.com --project="$FL_GOOGLE_PROJECT"
   ```

3. Create a dedicated service account in that project. Do not enable domain-wide delegation
   or impersonate your own email. Record its email, for example
   `fl-group-reader@YOUR_PROJECT_ID.iam.gserviceaccount.com`.
4. Create the user/operator/admin groups described below. As their owner, **directly add** the
   service-account email to each configured group and give it the **Owner** role. Do not send
   an invitation requiring the service account to sign in. Keep yourself as an owner too.
5. Create/download a JSON key for this service account. Install it on the scheduler as
   `/etc/fl/google-groups.json`, owned by `fl-scheduler`, mode `0600`. Use a regular file;
   symlinks and files owned by another account are rejected.
6. Configure:

   ```ini
   FL_GOOGLE_GROUPS_BACKEND=cloud_identity
   FL_GOOGLE_GROUPS_CREDENTIALS=/etc/fl/google-groups.json
   ```

This backend requests only `https://www.googleapis.com/auth/cloud-identity.groups.readonly`.
It does not need `FL_GOOGLE_DELEGATED_ADMIN`, `FL_GOOGLE_DIRECTORY_CREDENTIALS`, Admin SDK,
or Admin-console authorization of the numeric client ID. It checks **direct human membership**;
add each person directly to a role group rather than relying on nested groups. Being a group
owner also counts as membership. A service-account owner itself cannot log in as a human.

Your Workspace policy must permit adding the service-account email (which is outside your
Workspace email domain), and your Cloud project must permit service-account key creation.
Group ownership cannot override an organization policy. If either action is blocked, ask the
organization administrator for that specific permission; super-admin domain-wide delegation
is not a requirement of this backend. Groups for Business must already be enabled; enabling
it or changing domain sharing policy requires Workspace administrator access.

See Google's [Groups API authentication setup](https://docs.cloud.google.com/identity/docs/how-to/setup),
[supported groups](https://docs.cloud.google.com/identity/docs/groups),
[group lookup](https://docs.cloud.google.com/identity/docs/reference/rest/v1/groups/lookup),
and [membership resources and expiry](https://docs.cloud.google.com/identity/docs/reference/rest/v1/groups.memberships).

### Optional delegated Workspace Directory service account

Existing deployments can keep the Directory backend. This alternative requires an
administrator-authorized delegation; skip it when using the group-owner option above.

1. Enable **Admin SDK API** (`admin.googleapis.com`) in the Google project.
2. Create a dedicated service account, enable/configure Workspace domain-wide delegation,
   and record its numeric OAuth client ID.
3. Create/download a JSON key for this account. Install it on the scheduler as
   `/etc/fl/google-directory.json`, owned by `fl-scheduler`, mode `0600`.
4. A Workspace super administrator authorizes that numeric client ID in the Admin console:
   **Security → Access and data control → API controls → Manage Domain Wide Delegation**.
   Grant exactly `https://www.googleapis.com/auth/admin.directory.group.member.readonly`.
5. Set `FL_GOOGLE_DELEGATED_ADMIN` to an actual Workspace administrator with permission to
   read group membership. Cloud IAM roles alone do not provide Workspace privileges.

Set `FL_GOOGLE_GROUPS_BACKEND=directory` (the compatibility default),
`FL_GOOGLE_DIRECTORY_CREDENTIALS=/etc/fl/google-directory.json`, and the delegated administrator
variable above. This backend requires its JSON key; ADC/WIF is not an implemented replacement.
Delegation changes can take time to propagate.
See [domain-wide delegation](https://developers.google.com/identity/protocols/oauth2/service-account)
and [Directory scopes](https://developers.google.com/workspace/admin/directory/v1/guides/authorizing).

### Groups and initial administrator

Create groups such as `fl-users@example.edu`, `fl-operators@example.edu`, and
`fl-admins@example.edu`. Add the initial deployer to the admin group before first login.
Set `FL_GOOGLE_USERS_GROUP`, `FL_GOOGLE_OPERATORS_GROUP`, and `FL_GOOGLE_ADMINS_GROUP` to their
actual addresses. The user group is required; the higher-role groups are optional in code,
but an admin group is needed for the normal enrollment UI. Admin/operator membership includes
lower-role permissions. Set `FL_GOOGLE_WORKSPACE_DOMAIN=example.edu` to restrict login to the
expected Workspace domain, or omit it for the application's broader supported identity policy.

The Cloud Identity backend resolves the group, looks up the direct member and verifies their
membership roles and expiry. The delegated backend uses Directory's
[`members.hasMember`](https://developers.google.com/workspace/admin/directory/reference/rest/v1/members/hasMember).
For first deployment, validate your own membership and non-member denial. Membership cache
expiry is at most 60 seconds; an expired positive result is not reused during provider outages.

## 4. DNS, TLS and firewall configuration

| Name | DNS resolution | TLS listener |
| --- | --- | --- |
| `scheduler.example.edu` | Scheduler's externally reachable address | Public HTTPS UI/API |
| `headscale.example.edu` | Scheduler's externally reachable address | Public coordinator HTTPS |
| `transfer.example.edu` | Gateway's externally reachable address | Public verification HTTPS |
| `agent.scheduler.example.edu` | Scheduler's allocated Headscale address | Private agent HTTPS/WSS |
| `gateway.internal.example.edu` | Gateway's allocated Headscale address | Private gateway control HTTPS |

The renderer supplies private records to Headscale. All names used for publicly trusted
certificates must be under a domain you control; private service names still need valid TLS.
Use DNS-01 certificate validation so private listeners need no public HTTP endpoint. Issue
each name separately, or otherwise align the certificate directories with the rendered paths.

Create the public DNS records from the operator workstation, choosing your DNS provider.
Run only the branch selected by `FL_DNS_PROVIDER`. Cloudflare or Cloud DNS can serve either
AWS or GCP VMs; this runbook's Route 53 renewal branch uses an EC2 instance role and therefore
requires AWS hosting. A zone must be authoritative before requesting certificates. If your
domain/subdomain is already managed by Cloudflare, retain that setup and use the Cloudflare
branch below. Leave the unused `FL_GCP_DNS_ZONE` and `FL_AWS_ZONE_ID` placeholders alone.

### Cloudflare DNS on AWS or GCP

Set `FL_DNS_PROVIDER=cloudflare` in `settings.env`. Find the **Zone ID** in the Cloudflare
dashboard for the existing authoritative domain, usually `example.edu` even when
`FL_DOMAIN=fl.example.edu`. Set `FL_CF_ZONE_ID` to that ID. You do not need a separate zone
for the deployment subdomain or any delegation to Google/AWS. An already delegated subdomain
zone should use its own authoritative zone instead of its parent.

In **My Profile → API Tokens → Create Token**, create a token with **Zone → DNS → Edit**
and **Zone → Zone → Read**, restricted to **Include → Specific zone → your authoritative zone**.
Store it once at the hidden prompt below. This token lets the operator create the three
service records and Certbot create/remove DNS-01 TXT challenges for automatic renewal.
Zone scope includes the other records in that zone; Cloudflare's API token is separate from
Google login/Groups credentials. If you restrict token source IPs, include your operator
workstation and both VMs' reserved external IPs; keep the token valid for renewals.

Keep all three service records **DNS only (gray cloud)** in this deployment. This exposes
the reserved VM address directly for Headscale and the gateway's SSH/BBCP ports. Private
agent/control records remain in Headscale; no public A records point to Headscale IPs.
Do not put Cloudflare Access browser challenges in front of these service endpoints.

**Operator commands, identical for AWS or GCP hosting:**

```sh
read -r -s -p 'Cloudflare zone-scoped API token: ' FL_INPUT
printf '\n'
printf 'dns_cloudflare_api_token = %s\n' "$FL_INPUT" > "$FL_STATE/certbot-cloudflare.ini"
unset FL_INPUT
chmod 600 "$FL_STATE/certbot-cloudflare.ini"
python3 - "$FL_STATE/certbot-cloudflare.ini" <<'PY'
import configparser
import ipaddress
import json
import os
import re
import sys
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request, urlopen
credentials = configparser.ConfigParser(interpolation=None)
credentials.read_string('[cloudflare]\n' + Path(sys.argv[1]).read_text())
token = credentials['cloudflare']['dns_cloudflare_api_token'].strip()
assert token and not any(c.isspace() for c in token)
zone_id = os.environ['FL_CF_ZONE_ID']
assert re.fullmatch('[0-9a-f]{32}', zone_id)
domain = os.environ['FL_DOMAIN'].lower()
base = f'https://api.cloudflare.com/client/v4/zones/{zone_id}'
def api(path='', payload=None):
    data = json.dumps(payload).encode() if payload is not None else None
    request = Request(base + path, data=data, headers={
        'Authorization': f'Bearer {token}', 'Content-Type': 'application/json'})
    with urlopen(request, timeout=30) as response:
        result = json.load(response)
    if not result.get('success'):
        raise RuntimeError(f"Cloudflare API failed: {result.get('errors')}")
    return result['result']
zone = api()['name'].lower()
assert domain == zone or domain.endswith('.' + zone), 'FL_DOMAIN is outside the selected zone'
records = []
for service in ('scheduler', 'headscale', 'transfer'):
    name = f'{service}.{domain}'
    ip = os.environ['FL_GATEWAY_PUBLIC_IP' if service == 'transfer' else 'FL_SCHEDULER_PUBLIC_IP']
    ipaddress.IPv4Address(ip)
    # Check all names before creating any: do not duplicate/replace existing DNS records.
    if api('/dns_records?' + urlencode({'name': name})):
        raise RuntimeError(f'{name} already has records; review/update them in Cloudflare before continuing')
    records.append({'type': 'A', 'name': name, 'content': ip, 'ttl': 300, 'proxied': False})
for record in records:
    api('/dns_records', record)
    print(f"Created DNS-only A record: {record['name']} -> {record['content']}")
PY
```

Creation assumes fresh service names. If the names already exist, review their addresses and
proxy status in Cloudflare and skip the creation block once they match the deployment; do not
run it blindly after partial creation. The token stays in a mode-0600 file and is not passed
in shell/process arguments. It is installed root-only on both VMs for Certbot below.
Sources: [Cloudflare token setup](https://developers.cloudflare.com/fundamentals/api/get-started/create-token/),
[DNS record API](https://developers.cloudflare.com/api/resources/dns/subresources/records/methods/create/),
[DNS-only proxy status](https://developers.cloudflare.com/dns/proxy-status/),
and [Certbot Cloudflare credentials](https://certbot-dns-cloudflare.readthedocs.io/en/stable/).

### Route 53 DNS on AWS

**AWS / Route 53:**

```sh
aws route53 get-hosted-zone --id "$FL_AWS_ZONE_ID"
jq -n --arg domain "$FL_DOMAIN" --arg scheduler "$FL_SCHEDULER_PUBLIC_IP" \
  --arg gateway "$FL_GATEWAY_PUBLIC_IP" '{Changes:[
    {Action:"UPSERT",ResourceRecordSet:{Name:("scheduler."+$domain),Type:"A",TTL:300,ResourceRecords:[{Value:$scheduler}]}},
    {Action:"UPSERT",ResourceRecordSet:{Name:("headscale."+$domain),Type:"A",TTL:300,ResourceRecords:[{Value:$scheduler}]}},
    {Action:"UPSERT",ResourceRecordSet:{Name:("transfer."+$domain),Type:"A",TTL:300,ResourceRecords:[{Value:$gateway}]}}
  ]}' > "$FL_STATE/dns-records.json"
FL_DNS_CHANGE=$(aws route53 change-resource-record-sets --hosted-zone-id "$FL_AWS_ZONE_ID" \
  --change-batch "file://$FL_STATE/dns-records.json" --query ChangeInfo.Id --output text)
aws route53 wait resource-record-sets-changed --id "$FL_DNS_CHANGE"
```

### Cloud DNS on AWS or GCP

**GCP / Cloud DNS**, for a new deployment with these A records not already present:

```sh
gcloud dns managed-zones describe "$FL_GCP_DNS_ZONE" --project="$FL_GCP_PROJECT"
for FL_NAME in scheduler headscale transfer; do
  FL_IP=$FL_SCHEDULER_PUBLIC_IP
  if [ "$FL_NAME" = transfer ]; then FL_IP=$FL_GATEWAY_PUBLIC_IP; fi
  gcloud dns record-sets create "$FL_NAME.$FL_DOMAIN." --project="$FL_GCP_PROJECT" \
    --zone="$FL_GCP_DNS_ZONE" --type=A --ttl=300 --rrdatas="$FL_IP"
done
gcloud iam service-accounts create fl-certbot --project="$FL_GCP_PROJECT"
FL_CERTBOT_ACCOUNT="fl-certbot@$FL_GCP_PROJECT.iam.gserviceaccount.com"
gcloud projects add-iam-policy-binding "$FL_GCP_PROJECT" \
  --member="serviceAccount:$FL_CERTBOT_ACCOUNT" --role=roles/dns.reader
gcloud dns managed-zones get-iam-policy "$FL_GCP_DNS_ZONE" --project="$FL_GCP_PROJECT" \
  --format=json > "$FL_STATE/dns-zone-policy.json"
jq --arg member "serviceAccount:$FL_CERTBOT_ACCOUNT" \
  '.bindings = (.bindings // []) | if any(.bindings[]; .role == "roles/dns.admin" and (.condition == null))
   then .bindings |= map(if .role == "roles/dns.admin" and (.condition == null)
     then .members = ((.members + [$member]) | unique) else . end)
   else .bindings += [{role:"roles/dns.admin",members:[$member]}] end' \
  "$FL_STATE/dns-zone-policy.json" > "$FL_STATE/dns-zone-policy-updated.json"
gcloud dns managed-zones set-iam-policy "$FL_GCP_DNS_ZONE" --project="$FL_GCP_PROJECT" \
  --policy-file="$FL_STATE/dns-zone-policy-updated.json"
gcloud iam service-accounts keys create "$FL_STATE/certbot-google.json" \
  --project="$FL_GCP_PROJECT" --iam-account="$FL_CERTBOT_ACCOUNT"
chmod 600 "$FL_STATE/certbot-google.json"
```

The GCP renewal identity gets project-level DNS read access and zone-level DNS administration,
preserving the zone policy's other bindings and etag. Its key is distinct from the group reader.
Sources: [Route 53 record changes](https://docs.aws.amazon.com/cli/latest/reference/route53/change-resource-record-sets.html),
[Cloud DNS records](https://docs.cloud.google.com/sdk/gcloud/reference/dns/record-sets/create),
[zone IAM](https://docs.cloud.google.com/dns/docs/zones/iam-per-resource-zones),
[Certbot Google permissions](https://certbot-dns-google.readthedocs.io/en/stable/).

### TLS installation for the selected DNS provider

**Both hosting providers:** install certificate tooling and issue certificates before Headscale
bootstrap. Create the host's protected parameter file and upload the selected DNS credential:

```sh
printf 'FL_DNS_PROVIDER=%q\nFL_DOMAIN=%q\n' "$FL_DNS_PROVIDER" "$FL_DOMAIN" > "$FL_STATE/host.env"
for FL_ROLE in scheduler gateway; do
  flssh "$FL_ROLE" 'install -d -m 700 ~/fl-secrets'
  flput "$FL_ROLE" "$FL_STATE/host.env" fl-secrets/host.env
  if [ "$FL_DNS_PROVIDER" = cloudflare ]; then
    flput "$FL_ROLE" "$FL_STATE/certbot-cloudflare.ini" fl-secrets/certbot-cloudflare.ini
  elif [ "$FL_DNS_PROVIDER" = gcp ]; then
    flput "$FL_ROLE" "$FL_STATE/certbot-google.json" fl-secrets/certbot-google.json
  fi
done
for FL_ROLE in scheduler gateway; do
  flssh "$FL_ROLE" "bash -se -- $FL_ROLE" <<'HOST'
source "$HOME/fl-secrets/host.env"
FL_ROLE=$1
sudo apt-get update
sudo apt-get install -y nginx certbot python3-certbot-dns-google python3-certbot-dns-route53 \
  python3-certbot-dns-cloudflare
sudo install -d -m 755 /etc/fl
if [ "$FL_DNS_PROVIDER" = cloudflare ]; then
  sudo install -m 600 "$HOME/fl-secrets/certbot-cloudflare.ini" /etc/fl/certbot-cloudflare.ini
  FL_PLUGIN=(--dns-cloudflare --dns-cloudflare-credentials /etc/fl/certbot-cloudflare.ini \
    --dns-cloudflare-propagation-seconds 60)
elif [ "$FL_DNS_PROVIDER" = gcp ]; then
  sudo install -m 600 "$HOME/fl-secrets/certbot-google.json" /etc/fl/certbot-google.json
  FL_PLUGIN=(--dns-google --dns-google-credentials /etc/fl/certbot-google.json)
elif [ "$FL_DNS_PROVIDER" = aws ]; then
  FL_PLUGIN=(--dns-route53)
else
  printf 'Unknown FL_DNS_PROVIDER\n' >&2; exit 1
fi
if [ "$FL_ROLE" = scheduler ]; then
  FL_NAMES=("scheduler.$FL_DOMAIN" "headscale.$FL_DOMAIN" "agent.scheduler.$FL_DOMAIN")
else
  FL_NAMES=("transfer.$FL_DOMAIN" "gateway.internal.$FL_DOMAIN")
fi
for FL_NAME in "${FL_NAMES[@]}"; do
  sudo certbot certonly --non-interactive --agree-tos \
    --register-unsafely-without-email "${FL_PLUGIN[@]}" -d "$FL_NAME"
done
sudo install -d -m 755 /etc/letsencrypt/renewal-hooks/deploy
printf '#!/bin/sh\nsystemctl reload nginx\n' | \
  sudo tee /etc/letsencrypt/renewal-hooks/deploy/fl-nginx >/dev/null
sudo chmod 755 /etc/letsencrypt/renewal-hooks/deploy/fl-nginx
sudo systemctl enable --now certbot.timer
sudo certbot renew --dry-run --run-deploy-hooks
HOST
done
```

Unattended Cloudflare renewal uses the protected API token, Route 53 renewal uses the EC2
instance role through IMDSv2, and Cloud DNS renewal uses the protected Google DNS key.
This example registers without an email; set an operational account email
with `--email` instead if desired. nginx must be running by the time the deploy hook is tested.

The provider commands above also install the appropriate certificate plugin and unattended
renewal hook. Test renewals again after final nginx installation with
`flssh scheduler 'sudo certbot renew --dry-run --run-deploy-hooks'` and
`flssh gateway 'sudo certbot renew --dry-run --run-deploy-hooks'`.

Cloud and host firewalls must agree. Allow public TCP 443 to the coordinator/scheduler and
gateway; allow gateway TCP 22 and the configured BBCP range (default 5000–5099). Restrict
administrative access to the management path. Keep PostgreSQL and application ports 8080/8081
off the public network. Macs initiate scheduler and transfer connections; no public Mac listener
or incoming Mac SSH service is required. Permit the Tailscale transport needed for your topology;
test both direct traffic and relay fallback. The shipped Headscale config uses external DERP
servers and does not enable an embedded production DERP server.

On GCP, use [the NIC/address settings](#gcp-nic-and-vm-creation-settings) and
[per-role firewall rules](#gcp-firewall-rules) in section 2. Remove the distro's default nginx
site when installing the rendered fragments so its wildcard/HTTP listeners do not add services
outside this plan.

The public BBCP payload hop is unencrypted. SSH protects its bootstrap, SHA checks verify
integrity, and Headscale encrypts the private hop. Account for this when deciding which artifacts
may cross the public transfer endpoint.

## 5. Linux checkout and service accounts

**AWS or GCP operator commands:** the `flssh`/`flput` helpers choose `ssh`/`scp` or
`gcloud compute ssh`/`scp`. Run from your repository checkout. Upload an archive of the
reviewed commit, plus non-secret settings and cloud-resource identifiers:

```sh
git cat-file -e "$FL_REPO_REV^{commit}"
git archive --format=tar "$FL_REPO_REV" > "$FL_STATE/source.tar"
flsave
for FL_ROLE in scheduler gateway; do
  flssh "$FL_ROLE" 'install -d -m 700 ~/fl-secrets'
  flput "$FL_ROLE" "$FL_STATE/settings.env" fl-secrets/settings.env
  flput "$FL_ROLE" "$FL_STATE/resources.env" fl-secrets/resources.env
  flput "$FL_ROLE" "$FL_STATE/source.tar" fl-secrets/source.tar
done
```

Install the Ubuntu packages and initialize **only the newly provisioned data disk**. GCP
identifies it through its device-name symlink; AWS identifies the EBS volume through its NVMe
serial, avoiding guesses about `/dev/nvme1n1`. The guard rejects partitioned/unknown-formatted
disks. The scheduler disk backs PostgreSQL and Headscale through bind mounts; the gateway disk
backs `/var/lib/fl-transfer`. Keep all mounts required at boot, so missing disks stop dependent
services rather than silently writing to the boot disk.

```sh
for FL_ROLE in scheduler gateway; do
  flssh "$FL_ROLE" "bash -se -- $FL_ROLE" <<'HOST'
set -o pipefail
source "$HOME/fl-secrets/settings.env"
source "$HOME/fl-secrets/resources.env"
FL_ROLE=$1
sudo apt-get update
sudo apt-get install -y nginx openssh-server git curl jq python3 python3-venv \
  build-essential pkg-config libssl-dev zlib1g-dev libnsl-dev certbot \
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
```

The Pixi binary is pinned to `FL_PIXI_VERSION`; the bootstrap uses the official installer.
Record `/usr/local/bin/pixi --version` and retain the installer with your deployment records
for an exact bootstrap repeat. Application dependencies remain
locked by the checked-out `pixi.lock`. For a private repository, the archive upload avoids
placing repository credentials on the VMs.

On **either provider**, move gateway administrative SSH to port 2222 before opening public
transfer SSH. Keep your existing session open and verify a second login. These commands target
Ubuntu 24.04's OpenSSH service and socket setup:

```sh
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
```

Verify there is no OS administrative listener on port 22 before continuing to section 6. Sources:
[EBS NVMe identification](https://docs.aws.amazon.com/ebs/latest/userguide/identify-nvme-ebs-device.html),
[Pixi installation](https://pixi.prefix.dev/latest/installation/).

Keep `/opt/fl`, `.pixi` and installed tools administrator-owned and readable/executable by
the service accounts. `/etc/fl` is root-owned; systemd reads root-owned mode-0600 environment
files. Service-read keys/JSON must be owned by their service account and mode 0600. Keep real
secrets outside the checkout, deployment YAML, job YAML and cluster YAML.

## 6. Headscale bootstrap and credentials

**AWS or GCP operator commands:** install the pinned network binaries and official Linux
Tailscale systemd unit on both VMs, then bootstrap only the coordinator's public TLS listener.
These blocks use the selected provider's SSH transport and the real NIC addresses saved in
section 2; no Headscale address is guessed.

```sh
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
sudo headscale --config /etc/headscale/config.yaml configtest
sudo systemctl daemon-reload
sudo systemctl enable --now headscale
sudo nginx -t
sudo systemctl restart nginx
HOST
curl --fail "https://headscale.$FL_DOMAIN/health"
```

Issue one short-lived infrastructure key at a time and join immediately. Store keys in
protected files, never shell arguments. Tagged keys require no human Headscale user.

```sh
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
```

Protect and back up the enrollment key; reusing the deployment requires preserving it.
API key expiration is 30 days here, so schedule rotation before that deadline. Native Mac
enrollment issues its own distinct node keys later.

The coordinator stays on loopback behind nginx, uses tagged nonreusable infrastructure keys,
and publishes only the configured private names/ACLs. Its embedded DERP server is disabled;
external DERP provides fallback. No subnet or exit routes are advertised. Preserve the SQLite
database and Noise key together. See [Headscale operations](headscale-operations.md).

## 7. Render and install deployment bundles

**AWS or GCP operator commands:** obtain the gateway's public host key, generate a manifest
from the saved addresses, render on the scheduler, and install each role's bundle. The private
host key remains on the gateway. This is a fresh deployment; choose a new bundle path for
subsequent renders rather than overwriting an existing bundle.

```sh
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
                'host_key': key, 'data_port_first': 5000, 'data_port_last': 5099},
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
```

The manifest is JSON (valid YAML input) so the workstation needs no PyYAML dependency.
nginx can briefly return 502 for application paths until sections 8/9 start their upstreams;
Headscale remains available. Both private addresses must exist before nginx validation.

Review the generated bundle before installation. `/etc/headscale` files use root/headscale
ownership and mode 0640; transfer endpoint JSON is owned by `fl-scheduler`, mode 0600.
The renderer refuses an existing destination and keeps fixed listener/data ports, private
DNS, ACLs and SSH host-key pins consistent. `license_relay: null` emits no relay deployment.
See [Linux deployment](linux-deployment.md) for native validators and optional relay rendering.

## 8. Transfer gateway

**AWS or GCP operator commands:** transfer the shared control secret, install BBCP, the API
environment and dedicated SSH unit, validate, and start. The public transfer firewall opens
only after administrative SSH on port 2222 and the forced-command daemon on port 22 work.

```sh
flput gateway "$FL_STATE/gateway-control-secret" fl-secrets/gateway-control-secret
flssh gateway 'bash -se' <<'HOST'
cd /opt/fl
sudo pixi run python tests/build_bbcp.py --directory /tmp/fl-bbcp
sudo install -d -m 755 /opt/fl-tools
sudo install -m 755 /tmp/fl-bbcp/bbcp /opt/fl-tools/bbcp
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
```

**AWS:** open the scoped transfer SSH and BBCP payload range after that verification:

```sh
aws ec2 authorize-security-group-ingress --group-id "$FL_AWS_GATEWAY_SG" \
  --protocol tcp --port 22 --cidr 0.0.0.0/0
aws ec2 authorize-security-group-ingress --group-id "$FL_AWS_GATEWAY_SG" \
  --protocol tcp --port 5000-5099 --cidr 0.0.0.0/0
aws ec2 revoke-security-group-ingress --group-id "$FL_AWS_GATEWAY_SG" \
  --protocol tcp --port 22 --cidr "$FL_ADMIN_CIDR"
```

**GCP:**

```sh
gcloud compute firewall-rules create fl-gateway-transfer --project="$FL_GCP_PROJECT" \
  --network=fl-vpc --direction=INGRESS --priority=1000 --action=ALLOW \
  --target-tags=fl-transfer-gateway --source-ranges=0.0.0.0/0 --rules=tcp:22,tcp:5000-5099
gcloud compute firewall-rules update fl-bootstrap-admin --project="$FL_GCP_PROJECT" \
  --target-tags=fl-scheduler
```

The earlier firewall table and CLI snippet describe this same final rule; run its creation
only once. Transfer SSH continues to allow only `fl-transfer` with service-generated keys,
no shell, password, TTY or forwarding.

The gateway's HTTP API binds to loopback; nginx exposes separate public verification and
private control listeners. Scoped BBCP uses dedicated SSH on port 22 and data ports 5000–5099.
The gateway control credential must match the scheduler's protected value. The public BBCP
payload hop is unencrypted; the Headscale hop is encrypted and independent SHA checks gate
execution. Input retention is one day from first upload grant; exported results have their
own deadlines. See [gateway operations](transfer-gateway.md).

## 9. PostgreSQL, scheduler and dashboard

**AWS or GCP operator commands:** upload the protected application credentials and install
PostgreSQL 17 from PostgreSQL's Ubuntu repository on the scheduler. All credential values come
from files created in sections 3/6, so they do not appear in process arguments.

```sh
for FL_FILE in google-groups.json google-client-id google-client-secret \
  headscale-api-key gateway-control-secret enrollment-encryption-key postgres-password; do
  flput scheduler "$FL_STATE/$FL_FILE" "fl-secrets/$FL_FILE"
done
flssh scheduler 'bash -se' <<'HOST'
source "$HOME/fl-secrets/settings.env"
sudo install -d -m 755 /usr/share/postgresql-common/pgdg
sudo curl -fsSL https://www.postgresql.org/media/keys/ACCC4CF8.asc \
  -o /usr/share/postgresql-common/pgdg/apt.postgresql.org.asc
printf 'deb [signed-by=/usr/share/postgresql-common/pgdg/apt.postgresql.org.asc] https://apt.postgresql.org/pub/repos/apt noble-pgdg main\n' | \
  sudo tee /etc/apt/sources.list.d/pgdg.list >/dev/null
sudo apt-get update
sudo apt-get install -y postgresql-17 postgresql-client-17
sudo systemctl enable --now postgresql
sudo install -m 600 -o fl-scheduler -g fl-scheduler \
  "$HOME/fl-secrets/google-groups.json" /etc/fl/google-groups.json
sudo /opt/fl/.pixi/envs/default/bin/python - "$HOME/fl-secrets" "$HOME/fl-bundle" "$FL_WORKSPACE_DOMAIN" <<'PY'
import os
import re
import sys
from pathlib import Path
from urllib.parse import quote
os.umask(0o077)
secret, bundle = map(Path, sys.argv[1:3])
def read(name):
    return (secret / name).read_text().strip()
password = read('postgres-password')
assert re.fullmatch('[0-9a-f]{64}', password)
# These are fresh-database commands; do not run CREATE ROLE/DB again on an existing deployment.
sql = f"CREATE ROLE fl_scheduler LOGIN PASSWORD '{password}';\nCREATE DATABASE fletcherlake OWNER fl_scheduler;\n"
sql_path = Path('/etc/fl/create-database.sql')
sql_path.write_text(sql)
sql_path.chmod(0o600)
domain = sys.argv[3]
values = {
    'FL_DATABASE_URL': f'postgresql+psycopg://fl_scheduler:{quote(password, safe="")}@127.0.0.1:5432/fletcherlake',
    'FL_GOOGLE_CLIENT_ID': read('google-client-id'),
    'FL_GOOGLE_CLIENT_SECRET': read('google-client-secret'),
    'FL_GOOGLE_GROUPS_BACKEND': 'cloud_identity',
    'FL_GOOGLE_GROUPS_CREDENTIALS': '/etc/fl/google-groups.json',
    'FL_GOOGLE_WORKSPACE_DOMAIN': domain,
    'FL_GOOGLE_USERS_GROUP': f'fl-users@{domain}',
    'FL_GOOGLE_OPERATORS_GROUP': f'fl-operators@{domain}',
    'FL_GOOGLE_ADMINS_GROUP': f'fl-admins@{domain}',
    'FL_HEADSCALE_API_KEY': read('headscale-api-key'),
    'FL_ENROLLMENT_ENCRYPTION_KEY': read('enrollment-encryption-key'),
    'FL_TRANSFER_GATEWAY_CONTROL_SECRET': read('gateway-control-secret'),
}
def quoted(value):
    if any(c in value for c in '\r\n\0'):
        raise ValueError('Environment values must be single-line strings')
    return '"' + value.replace('\\', '\\\\').replace('"', '\\"') + '"'
text = (bundle / 'scheduler/environment.defaults').read_text()
text += ''.join(f'{key}={quoted(value)}\n' for key, value in values.items())
target = Path('/etc/fl/scheduler.env')
target.write_text(text)
target.chmod(0o600)
PY
sudo cat /etc/fl/create-database.sql | sudo -u postgres psql -q --set=ON_ERROR_STOP=1
sudo rm /etc/fl/create-database.sql
cd /opt/fl
sudo pixi run -e web web-install
sudo pixi run -e web web-check
sudo pixi run -e web web-test
sudo pixi run -e web web-build
sudo chmod -R a+rX /opt/fl/services/dashboard/dist
sudo systemd-run --wait --pipe --collect \
  --property=User=fl-scheduler --property=WorkingDirectory=/opt/fl \
  --property=EnvironmentFile=/etc/fl/scheduler.env \
  /opt/fl/.pixi/envs/default/bin/alembic -c /opt/fl/services/scheduler/alembic.ini upgrade head
sudo install -d -m 755 /etc/systemd/system/fl-scheduler.service.d
printf '[Unit]\nRequiresMountsFor=/var/lib/postgresql\nAfter=postgresql.service\nWants=postgresql.service\n' | \
  sudo tee /etc/systemd/system/fl-scheduler.service.d/20-data.conf >/dev/null
sudo systemd-analyze verify /etc/systemd/system/fl-scheduler.service
sudo nginx -t
sudo systemctl daemon-reload
sudo systemctl enable --now fl-scheduler
sudo systemctl reload nginx
HOST
curl --fail "https://scheduler.$FL_DOMAIN/healthz"
```

The example uses exactly `fl-users`, `fl-operators`, `fl-admins` in your Workspace domain.
Change the environment generator if you used other group names. All configured groups must
be readable by the service-account owner, even if the human administrator belongs to just
the highest-role group. PostgreSQL stays local; no RDS/Cloud SQL deployment is needed.
Source: [PostgreSQL Ubuntu repository](https://www.postgresql.org/download/linux/ubuntu/).

Sign into `https://scheduler.$FL_DOMAIN/clusters` using the initial administrator and check
`/admin/clusters` and `/admin/users`. Notification, placement, enrollment, transfer and export
workers run inside the scheduler; no separate worker VM is required. Startup requires Alembic
revision `0010_notification_delivery` rather than migrating implicitly. For future upgrades,
stop old scheduler instances before migration and start the matching new binary afterwards.
Agents continue running and replay receipts after reconnection.

RDS/Cloud SQL can replace local PostgreSQL as a separate deployment choice; configure private
connectivity, TLS and credentials explicitly. Their IAM/proxy startup is not an application
feature, and these commands deliberately deploy local PostgreSQL. See
[scheduler operations](scheduler-operations.md) and [dashboard operations](scheduler-dashboard.md).

## 10. Mac agents, firmware and local interfaces

**AWS and GCP use the same commands on each physical Mac below.** Cloud provisioning does
not install firmware or provide board/JTAG access on a Mac. Before enrollment, verify the
selected cloud scheduler from your operator shell:

```sh
flssh scheduler 'systemctl is-active headscale fl-scheduler nginx'
curl --fail "https://scheduler.$FL_DOMAIN/healthz"
```

Use `https://scheduler.$FL_DOMAIN` as the scheduler URL in the Mac commands (set `FL_DOMAIN`
in that Mac's shell too). Hardware mappings, firmware argv and the enrollment ticket are
deployment-specific inputs; supply them as described below rather than copying mock inventory
unchanged. Generate the ticket in the administrator dashboard after the first Google login.

Copy the reviewed source archive from the operator workstation to each Mac. Enable macOS
Remote Login for the administrator account first, or copy the same archive locally:

```sh
FL_MAC_SSH=REPLACE_WITH_ADMIN_USER@REPLACE_WITH_MAC_HOST
scp "$FL_STATE/source.tar" "$FL_MAC_SSH:fl-source.tar"
```

**On the Apple Silicon Mac**, use an administrator account. Install Homebrew and an active
Tailscale Mac app first; approve its macOS network extension and make its CLI available on
PATH. These are Mac prerequisites, shared by either hosting provider. Apple's command-line
tools prompt is interactive; finish it before running the second block:

```sh
xcode-select --install
```

If the tools are already installed, skip that command. Create a fresh `/opt/fl` checkout owned
by this trusted administrator, install the pinned Pixi binary and build the checksum-pinned BBCP:

```sh
set -euo pipefail
export FL_DOMAIN=REPLACE_WITH_THE_SAME_DEPLOYED_DOMAIN
export FL_PIXI_VERSION=v0.65.0
xcode-select -p
brew install openssl@3
sudo install -d -m 755 -o "$(id -un)" /opt/fl
tar -xf "$HOME/fl-source.tar" -C /opt/fl
curl -fsSL https://pixi.sh/install.sh -o /tmp/fl-pixi-install.sh
sudo env PIXI_VERSION="$FL_PIXI_VERSION" PIXI_HOME=/opt/pixi PIXI_BIN_DIR=/usr/local/bin \
  PIXI_NO_PATH_UPDATE=1 bash /tmp/fl-pixi-install.sh
cd /opt/fl
pixi install --locked
pixi run python tests/build_bbcp.py --directory /tmp/fl-bbcp \
  --openssl-prefix "$(brew --prefix openssl@3)"
sudo install -d -m 755 /opt/fl-tools
sudo install -m 755 /tmp/fl-bbcp/bbcp /opt/fl-tools/bbcp
export PATH="/opt/fl-tools:/usr/local/bin:$PATH"
tailscale version
```

Set `environment.bbcp.path: /opt/fl-tools/bbcp` in the cluster overrides so launchd can find
it independently of your shell's PATH. Record the installed macOS, Tailscale and OpenSSL
versions with your inventory. Homebrew's OpenSSL formula can change; reproduce its recorded
version when rebuilding the same native toolchain.
Source: [Homebrew OpenSSL](https://formulae.brew.sh/formula/openssl@3).

For Vivado Lab, keep `license_relay` disabled. However, AMD's published supported-OS table
lists Windows/Linux, not native macOS. Validate your chosen tool environment or wrapper and
its board/JTAG access; this repository does not install a Mac-native Vivado package. See
[AMD installer/OS support](https://www.amd.com/en/support/adaptive-socs-and-fpgas/installer-info-general.html).
Autodetection searches `vivado`; set `environment.vivado.path` and `edition: lab` explicitly
when using `vivado_lab` or a wrapper. Inventory tool metadata alone does not execute hardware.

Create a cluster override YAML with one to three explicit board slots. Start from the shape in
`examples/cluster.yaml`, replacing simulated boards with `backend: lilikoi`, actual device
mappings and supplied `firmware_commands`. There are no guessed firmware command names.
Before running production jobs, validate power on/off, FPGA programming, SoC loading, start,
stop, UART, completion and any requested voltage/frequency/sensor operations. Commands are
argv arrays with deadlines; completion emits a JSON `RunResult`. Full contract:
[Mac operations](macos-operations.md#firmware-adapter-contract).

In the scheduler dashboard, issue a short-lived ticket from `/admin/clusters`. On the Mac:

```sh
cd /opt/fl
pixi run fl cluster setup init /path/to/cluster-overrides.yaml \
  --scheduler "https://scheduler.$FL_DOMAIN"
# Enter the enrollment token at the hidden prompt.
pixi run fl cluster setup confirm /opt/fl
pixi run fl cluster status
pixi run fl cluster status --dashboard
```

Init joins Headscale automatically and stores credentials separately from configuration.
Confirm installs the system launchd daemon and waits for readiness. State is under
`/Library/Application Support/fl`; cache/socket/locks are under `/var/run/fl`. The same
environment provides the Python SDK, `from fl import Cluster, ClusterSetup`.
No Google, cloud, Headscale admin or notification credentials belong on the Mac.

Use `fl cluster setup reconfigure /opt/fl --config /path/to/new.yaml` for configuration changes.
`fl cluster restart` drains hardware before a Mac reboot; `fl cluster destroy` permanently
retires the registration and preserves retention cleanup. These are lifecycle actions, not
routine status commands. Recovery details: [Mac operations](macos-operations.md).

## 11. Remote client

**AWS and GCP share this user-side workflow.** Set `FL_DOMAIN` to the deployed domain in the
client's shell, and use the same public scheduler URL regardless of hosting provider:

```sh
pixi run fl-client login --scheduler "https://scheduler.$FL_DOMAIN"
```

The remaining common submission/result commands below require your actual job YAML and UUID;
those are job inputs, not cloud provisioning parameters.

On an ordinary Linux/Chipyard or Mac user host, use a checkout of the same reviewed revision,
Pixi and OpenSSH. On Ubuntu, install the BBCP compiler prerequisites if missing:

```sh
sudo apt-get update
sudo apt-get install -y build-essential libssl-dev zlib1g-dev libnsl-dev openssh-client
```

From that checkout, build BBCP into a user-owned directory. On macOS, use the Apple
command-line tools and Homebrew OpenSSL prerequisites above; no cloud credentials or
Headscale membership are needed for this client:

```sh
pixi install --locked
mkdir -p "$HOME/.local/bin"
if [ "$(uname -s)" = Darwin ]; then
  pixi run python tests/build_bbcp.py --directory "$HOME/.cache/fl-bbcp" \
    --openssl-prefix "$(brew --prefix openssl@3)"
else
  pixi run python tests/build_bbcp.py --directory "$HOME/.cache/fl-bbcp"
fi
install -m 755 "$HOME/.cache/fl-bbcp/bbcp" "$HOME/.local/bin/bbcp"
export PATH="$HOME/.local/bin:$PATH"
pixi run fl-client login --scheduler "https://scheduler.$FL_DOMAIN"
pixi run fl-client submit /path/to/job.yaml --follow
pixi run fl-client jobs
pixi run fl-client status JOB_UUID
pixi run fl-client logs JOB_UUID --follow
pixi run fl-client results JOB_UUID --output /path/to/results
```

Approve the printed terminal code through Google login in a browser on any machine. The client
saves protected, rotating scheduler credentials; it does not need cloud keys or a Google OAuth
client secret. Keep submission/download receipts with their identities to resume after lost
responses. See [remote client operation](remote-client.md).

## 12. Optional third-party integrations

### Mailgun

Create a Mailgun account and sending domain. Publish the domain's required SPF/DKIM records
and complete verification. Use the region matching that domain (`us` or `eu`). Create a
domain sending key, which permits sending for that domain; a read-only key cannot send.
Configure the domain, sender, key, operator recipients and optional owner delivery in the
notification JSON. Sandbox domains have recipient restrictions; validate with approved test
recipients before broad delivery. See [domain verification](https://documentation.mailgun.com/docs/mailgun/user-manual/domains/domains-verify)
and [Mailgun key management](https://documentation.mailgun.com/docs/mailgun/user-manual/api-key-mgmt/rbac-mgmt).

### Slack

Create a Slack app in the destination workspace. Enable **Incoming Webhooks**, select
**Add New Webhook to Workspace**, choose the channel and authorize installation. Store its
complete webhook URL in a `channel: slack` entry. The URL determines the destination and is
the credential; no bot token or interactive Slack OAuth handler is needed by this scheduler.
Workspace policy may require administrator approval. See
[Slack incoming webhooks](https://docs.slack.dev/messaging/sending-messages-using-incoming-webhooks/).

### Google Chat

Use a Chat space in a Workspace organization that permits incoming webhooks. In the space's
**Apps & integrations → Webhooks**, create an incoming webhook and save its full URL, including
`key` and `token`, in a `channel: google_chat` entry. This is separate from the scheduler's
Google OAuth/Groups credentials; a Google Chat bot service is not required. See
[Google Chat webhook setup](https://developers.google.com/workspace/chat/quickstart/webhooks).

### Enable notification workers

**AWS or GCP operator commands:** after obtaining the provider credentials/approvals described
above, prepare the protected JSON locally. The provider's Slack/Chat installation UI and
Mailgun domain verification are external prerequisites; neither AWS CLI nor gcloud creates
those accounts or bypasses workspace approval.

```sh
cp examples/notifications.json "$FL_STATE/notifications.json"
chmod 600 "$FL_STATE/notifications.json"
"${EDITOR:-vi}" "$FL_STATE/notifications.json"
```

Optionally store a copy in your cloud secret service and fetch it into the same protected
operator directory. Run one provider branch; creation assumes the secret name is new. For
rotation use AWS `put-secret-value` or GCP `secrets versions add` instead of creating again.

**AWS:**

```sh
aws secretsmanager create-secret --name fl/notifications \
  --secret-string "file://$FL_STATE/notifications.json"
aws secretsmanager get-secret-value --secret-id fl/notifications \
  --query SecretString --output text > "$FL_STATE/notifications.json"
```

**GCP:**

```sh
gcloud services enable secretmanager.googleapis.com --project="$FL_GCP_PROJECT"
gcloud secrets create fl-notifications --project="$FL_GCP_PROJECT" \
  --replication-policy=automatic --data-file="$FL_STATE/notifications.json"
gcloud secrets versions access latest --secret=fl-notifications --project="$FL_GCP_PROJECT" \
  --out-file="$FL_STATE/notifications.json"
```

Cloud secret APIs require the corresponding permissions on the operator identity; no new
VM runtime secret-reader privilege is necessary for this upload-based deployment.
For either cloud, install and enable the configuration:

```sh
flput scheduler "$FL_STATE/notifications.json" fl-secrets/notifications.json
flssh scheduler 'bash -se' <<'HOST'
sudo install -m 600 -o fl-scheduler -g fl-scheduler \
  "$HOME/fl-secrets/notifications.json" /etc/fl/notifications.json
if ! sudo grep -q '^FL_NOTIFICATION_CONFIG_FILE=' /etc/fl/scheduler.env; then
  printf 'FL_NOTIFICATION_CONFIG_FILE=/etc/fl/notifications.json\n' | \
    sudo tee -a /etc/fl/scheduler.env >/dev/null
fi
sudo systemctl restart fl-scheduler
sudo systemctl is-active fl-scheduler
HOST
```

Sources: [AWS secret creation](https://docs.aws.amazon.com/cli/latest/reference/secretsmanager/create-secret.html)
and [GCP secret creation](https://docs.cloud.google.com/sdk/gcloud/reference/secrets/create).

Copy `examples/notifications.json` into `/etc/fl/notifications.json`, replace placeholders,
and remove providers you do not want. The file must be regular, owned by `fl-scheduler`, mode
`0600`; symlinks/public files are rejected. Add
`FL_NOTIFICATION_CONFIG_FILE=/etc/fl/notifications.json` to the scheduler environment and
restart `fl-scheduler`. Omitting the variable disables notifications. Validate through a
controlled job event and inspect `/api/admin/notifications`; avoid exposing credential URLs
in logs. Provider failures retry durably. Details: [notifications](notifications.md).

### BWRC license forwarding

Skip this for the current Vivado Lab deployment. If another tool needs it, add a complete
`license_relay` mapping with the BWRC backend, relay private address, licensed hostname,
manager port and fixed vendor port. Join the additional host with `tag:license-relay`, render
a fresh bundle, install its HAProxy configuration/drop-in, and update the coordinator's policy
and private DNS. No broadly routed BWRC subnet is needed. Checkout/release acceptance is
required only when enabled. See [optional license relay](license-relay.md).

## 13. Acceptance, maintenance and troubleshooting

**AWS status commands:**

```sh
aws ec2 describe-instance-status --instance-ids "$FL_AWS_SCHEDULER_ID" "$FL_AWS_GATEWAY_ID" \
  --include-all-instances --output table
aws ec2 describe-security-groups --group-ids "$FL_AWS_SCHEDULER_SG" "$FL_AWS_GATEWAY_SG" \
  --query 'SecurityGroups[].{Name:GroupName,Ingress:IpPermissions}' --output json
```

**GCP status commands:**

```sh
gcloud compute instances describe fl-scheduler --project="$FL_GCP_PROJECT" \
  --zone="$FL_GCP_ZONE" --format='yaml(status,networkInterfaces,disks)'
gcloud compute instances describe fl-transfer-gateway --project="$FL_GCP_PROJECT" \
  --zone="$FL_GCP_ZONE" --format='yaml(status,networkInterfaces,disks)'
gcloud compute firewall-rules list --project="$FL_GCP_PROJECT" \
  --filter='network:fl-vpc' --format='table(name,direction,allowed,sourceRanges,targetTags)'
```

**Either provider**, from the operator shell:

```sh
curl --fail "https://scheduler.$FL_DOMAIN/healthz"
test "$(curl -sS -o /dev/null -w '%{http_code}' "https://scheduler.$FL_DOMAIN/api/agents/test")" = 404
test "$(curl -sS -o /dev/null -w '%{http_code}' "https://transfer.$FL_DOMAIN/internal/test")" = 404
flssh scheduler 'systemctl is-active headscale fl-scheduler nginx tailscaled; \
  sudo ss -lntup; findmnt /var/lib/postgresql; findmnt /var/lib/headscale'
flssh gateway 'systemctl is-active fl-transfer-gateway fl-transfer-sshd nginx tailscaled; \
  sudo ss -lntup; findmnt /var/lib/fl-transfer'
flssh gateway "tailscale ping $FL_SCHEDULER_HEADSCALE_IP"
flssh scheduler "tailscale ping $FL_GATEWAY_HEADSCALE_IP"
flssh scheduler 'sudo journalctl -u fl-scheduler -u headscale --since "10 minutes ago" --no-pager'
flssh gateway 'sudo journalctl -u fl-transfer-gateway -u fl-transfer-sshd --since "10 minutes ago" --no-pager'
```

Create protected backups on the operator workstation. The PostgreSQL dump uses a transactionally
consistent snapshot. The Headscale archive briefly stops the coordinator for a consistent
SQLite/Noise-key/config copy and restarts it via a shell trap; existing Mac execution continues.
The archive includes `/etc/fl` credentials and must be treated as secret-bearing data.

```sh
FL_BACKUP_STAMP=$(date -u +%Y%m%dT%H%M%SZ)
mkdir -p "$FL_STATE/backups/$FL_BACKUP_STAMP"
flssh scheduler 'sudo -u postgres pg_dump -Fc fletcherlake' \
  > "$FL_STATE/backups/$FL_BACKUP_STAMP/postgres.dump"
flssh scheduler 'sudo bash -se -c '\''trap "systemctl start headscale" EXIT; \
  systemctl stop headscale; tar -czf - /var/lib/headscale /etc/headscale /etc/fl /etc/letsencrypt'\''' \
  > "$FL_STATE/backups/$FL_BACKUP_STAMP/scheduler-state.tgz"
flssh gateway 'sudo tar -czf - /etc/fl /etc/letsencrypt' \
  > "$FL_STATE/backups/$FL_BACKUP_STAMP/gateway-config.tgz"
```

Optionally create a dedicated private backup bucket and upload those files. Set a globally
unique bucket name; creation is a one-time step, uploads can repeat. The AWS bucket command
below targets the default `us-west-2` configuration (for `us-east-1`, omit the location constraint).

**AWS:**

```sh
FL_BACKUP_BUCKET=REPLACE_WITH_UNIQUE_PRIVATE_BUCKET_NAME
aws s3api create-bucket --bucket "$FL_BACKUP_BUCKET" \
  --create-bucket-configuration "LocationConstraint=$AWS_DEFAULT_REGION"
aws s3api put-public-access-block --bucket "$FL_BACKUP_BUCKET" \
  --public-access-block-configuration 'BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true'
aws s3 cp "$FL_STATE/backups/$FL_BACKUP_STAMP/" \
  "s3://$FL_BACKUP_BUCKET/$FL_BACKUP_STAMP/" --recursive --sse AES256
```

**GCP:**

```sh
FL_BACKUP_BUCKET=REPLACE_WITH_UNIQUE_PRIVATE_BUCKET_NAME
gcloud storage buckets create "gs://$FL_BACKUP_BUCKET" --project="$FL_GCP_PROJECT" \
  --location="$FL_GCP_REGION" --uniform-bucket-level-access --public-access-prevention
gcloud storage cp --recursive "$FL_STATE/backups/$FL_BACKUP_STAMP/" "gs://$FL_BACKUP_BUCKET/"
```

The configuration archives preserve TLS certificates and the gateway's private SSH host key.
These backups do not copy gateway SQLite/payload state or Mac collateral; back those up
separately if required. Keep the saved operator state, reviewed source revision, OS image IDs,
gateway state and Mac state with the deployment's protected recovery records. Verify restoration
on a separate host before relying on the backup procedure.

Before enabling real boards, verify: public health/login, admin enrollment, Mac READY state,
private DNS/TLS, a mock terminal submission with ELF+bitstream and independent result retrieval,
owner/non-member denial, public rejection of agent/control routes, and notification delivery
for each enabled provider. Then validate actual firmware and Mac restart/sleep/wake behavior.
Tests establish Linux simulated/native-tool behavior; live cloud/Workspace/Mac/provider setup
is not automatically proven by a passing test suite. An intermittent public BBCP upload timeout
remains documented in [the audit](requirement-audit.md).

| Symptom | Check |
| --- | --- |
| OAuth redirect mismatch | Exact scheme/hostname/path, web client, `FL_PUBLIC_ORIGIN` |
| Group lookup unavailable | Selected backend's API enabled, protected JSON key, outbound HTTPS; Cloud Identity: service account is an owner of every configured group; Directory: delegated client ID/scope and admin privileges |
| Signed-in user forbidden | Direct membership in configured groups; audience/domain restriction |
| Scheduler refuses startup | Missing environment values, schema revision, Groups credential file ownership/mode, dashboard build |
| GCP requested IP outside subnetwork range | `10.80.0.x` requires the example `fl-subnet` (`10.80.0.0/24`); select it in both reservation and NIC forms, or use an address from the existing subnet's actual CIDR |
| nginx cannot bind private listener | Actual allocated Headscale address, tailscaled startup and final manifest |
| Gateway SSH denied | Host key pin, issued grant, correct source, account lock/shell, StrictModes, listener conflict |
| BBCP stalls | TCP data range in cloud/host firewall, matching manifest/ACL, dedicated range, credential deadline |
| Mac configuration incomplete | Correct the inventory and rerun setup confirm; do not forge the marker |

Inspect `systemctl status`/`journalctl -u` for Linux units; Mac logs are in the agent state's
`logs/` directory. Keep logs private and do not enable logging of OAuth query strings, secrets
or complete webhook URLs. Back up PostgreSQL, Headscale database/Noise key/configuration together,
the persistent encryption key, gateway host key/state and Mac SQLite/collateral using consistent
database backups. Protect those backups as secret-bearing data.

Rotate OAuth/service-account keys, Headscale API keys, cloud identities, Cloudflare DNS tokens and provider secrets
through their own providers, updating protected files and restarting affected services. Preserve
the enrollment encryption key unless deliberately handling outstanding receipts. Gateway host
key changes require regenerated/pinned endpoint metadata; do not bypass host-key checking.

For upgrades, deploy the same reviewed revision, stop old scheduler instances before migrations,
apply Alembic explicitly, rebuild the dashboard, and restart. Agents keep local authority through
scheduler outages. Reconfigure Macs through the management workflow. Retired Macs still need
their separate launchd retention job until collateral expires; see [retirement](retirement.md).
