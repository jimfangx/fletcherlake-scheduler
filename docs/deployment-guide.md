# Deployment guide

This guide deploys the current repository from a checkout, with Linux services and macOS
agents. Commands are examples for fresh hosts; replace example domains, paths and addresses.
Use the same reviewed repository revision on all hosts. The renderer prepares files for review
and installation; it does not provision cloud resources or modify running services.

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

## 2. AWS or GCP hosting

Choose one hosting branch, or use existing Linux hosts. Cloud credentials manage infrastructure,
DNS, backups or secret retrieval; the scheduler itself does not call AWS APIs or automatically
fetch AWS/GCP secrets. Materialize its configuration as the files/environment described below.

### AWS

Use an organization-approved AWS account and region. Authenticate the deployment operator
with an existing IAM Identity Center permission set:

```sh
aws configure sso --profile fl-admin
aws sso login --profile fl-admin
aws sts get-caller-identity --profile fl-admin
```

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
gcloud config set project YOUR_PROJECT_ID
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
FL_GCP_REGION=YOUR_VM_REGION
gcloud compute networks create fl-vpc --subnet-mode=custom
gcloud compute networks subnets create fl-subnet \
  --network=fl-vpc --region="$FL_GCP_REGION" --range=10.80.0.0/24
gcloud compute addresses create fl-scheduler-internal \
  --region="$FL_GCP_REGION" --subnet=fl-subnet --addresses=10.80.0.10
gcloud compute addresses create fl-gateway-internal \
  --region="$FL_GCP_REGION" --subnet=fl-subnet --addresses=10.80.0.11
```

Alternatively, to keep the `default` subnet, select **Automatic** for the VM's internal IPv4
address rather than entering `10.80.0.11`. For a stable internal address, reserve an available
address within that subnet's actual CIDR or promote the assigned address to static. Inspect
the subnet's range with:

```sh
gcloud compute networks subnets describe default \
  --region=YOUR_VM_REGION --format='value(ipCidrRange)'
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

For example, after moving administrative SSH and starting the dedicated transfer daemon, the
gateway's final public transfer rule can be created with:

```sh
gcloud compute firewall-rules create fl-gateway-transfer \
  --network=fl-vpc --direction=INGRESS --priority=1000 --action=ALLOW \
  --target-tags=fl-transfer-gateway --source-ranges=0.0.0.0/0 \
  --rules=tcp:22,tcp:5000-5099
```

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

### OAuth web client

1. Select/create the Google Cloud project used for this application.
2. In **Google Auth platform → Branding**, configure the app name, support email and contacts.
   Select an Internal audience if every user is in your Workspace; otherwise configure the
   External audience and its test/publishing requirements.
3. Create a **Web application** client under **Google Auth platform → Clients**.
4. Register the exact redirect URI `https://scheduler.example.edu/api/auth/callback`, replacing
   the hostname. No localhost terminal callback or television/device OAuth client is needed.
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
   gcloud services enable cloudidentity.googleapis.com --project YOUR_PROJECT_ID
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

For Route 53, install Certbot's DNS plugin and grant its identity `route53:ListHostedZones`,
`route53:GetChange`, and zone-scoped `route53:ChangeResourceRecordSets`. Run with the credential
identity available to the renewal process, not merely your interactive shell:

```sh
sudo certbot certonly --dns-route53 -d scheduler.example.edu
```

For Cloud DNS, install the Google DNS plugin. Give its separate identity the documented
zone-editor and project zone-discovery permissions. With a protected JSON key outside GCP:

```sh
sudo certbot certonly --dns-google \
  --dns-google-credentials /etc/fl/certbot-google.json -d scheduler.example.edu
```

Repeat for the other names on the host serving them. VM identity/ADC can be used by the Google
plugin where configured. Set up unattended renewal, test it with `certbot renew --dry-run`,
and reload nginx after successful renewal. See the official
[Route 53 plugin](https://certbot-dns-route53.readthedocs.io/en/stable/) and
[Google DNS plugin](https://certbot-dns-google.readthedocs.io/en/stable/) instructions.

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

Install nginx, OpenSSH server, a C/C++ toolchain, make, OpenSSL/zlib/libnsl development packages,
and Pixi on the appropriate Linux hosts. Use the
[Pixi installation instructions](https://pixi.prefix.dev/latest/installation/).
Copy the reviewed checkout into `/opt/fl`; install as the deployment administrator:

```sh
cd /opt/fl
pixi install --locked
```

Keep the checkout, `.pixi` environment and tools administrator-owned and readable/executable
by the service accounts. Do not install production executables under an administrator's home;
the systemd units enable `ProtectHome`. Do not give services write access to application code.

On the scheduler host, create `headscale` and `fl-scheduler` system accounts. On the gateway,
create `fl-transfer` with home `/var/lib/fl-transfer` and a normal shell such as `/bin/sh`.
Its home/state directory must be owned by `fl-transfer` and mode `0700`. It authenticates only
with service-generated forced-command keys. Ensure the account permits public-key login with
the shipped `UsePAM no` configuration; a locked account can be rejected before key authentication.
See the [OpenSSH daemon manual](https://man.openbsd.org/sshd).

Create `/etc/fl` on both hosts, root-owned with traversable permissions. Service environment
files are root-owned mode `0600` and read by systemd. Service-read JSON/credential files are
owned by their service account and mode `0600`. Keep all real secrets outside the checkout,
deployment YAML, job YAML and cluster YAML.

## 6. Headscale bootstrap and credentials

On Linux x86-64, the repository downloader verifies its pinned Headscale and Tailscale artifacts:

```sh
cd /opt/fl
pixi run python tests/download_network_tools.py --directory /tmp/fl-network
sudo install -m 0755 /tmp/fl-network/headscale /usr/local/bin/headscale
```

Install/start a system-managed Tailscale client on both Linux hosts and on the Macs. The
download helper is not a tailscaled service installer. See
[Tailscale's Linux installation instructions](https://tailscale.com/docs/install/linux) and
[Headscale operations](headscale-operations.md) for client compatibility and pinned versions.
The clients join this Headscale coordinator; no hosted Tailscale API token is required.

Bootstrap the coordinator before its private address exists:

1. Install `services/headscale/config.yaml` and `policy.json` into `/etc/headscale`, root-owned,
   group `headscale`, mode `0640`; directory mode `0750`. Set `server_url` to your actual public
   coordinator origin. Keep its REST/metrics/gRPC bindings on loopback.
2. Install `services/headscale/headscale.service` under `/etc/systemd/system`. Its state is
   `/var/lib/headscale`; systemd manages its protected state/runtime directories.
3. Install only the Headscale nginx fragment initially. Replace `PUBLIC_IP`, hostname and
   certificate paths. The bind address must exist on the local NIC. Do not yet install the
   scheduler's private listener with guessed Headscale addresses.
4. Validate, start Headscale and start/reload nginx:

```sh
sudo /usr/local/bin/headscale --config /etc/headscale/config.yaml configtest
sudo systemctl daemon-reload
sudo systemctl enable --now headscale
sudo nginx -t
sudo systemctl enable --now nginx
```

Create a nonreusable ten-minute key for each infrastructure node on the coordinator:

```sh
sudo -u headscale headscale --config /etc/headscale/config.yaml preauthkeys create \
  --tags tag:scheduler --expiration 10m
```

Transfer the key through a protected file, then run on that node:

```sh
sudo tailscale up --login-server https://headscale.example.edu \
  --auth-key file:/path/to/protected-key --accept-routes=false --accept-dns=true
sudo tailscale ip -4
```

Delete the temporary key file after joining. Repeat with `tag:transfer-gateway` for the gateway.
Only issue `tag:license-relay` if enabling that optional host. Do not use reusable/ephemeral
keys or advertise subnet/exit routes. Record actual allocated addresses for the final manifest.

Create a Headscale administrative API key for the scheduler:

```sh
sudo -u headscale headscale --config /etc/headscale/config.yaml apikeys create --expiration 720h
```

Save its one-time value as `FL_HEADSCALE_API_KEY` in the protected scheduler environment and
schedule rotation before expiry. It is distinct from node join keys and Mac enrollment tokens.
Generate the persistent enrollment encryption key and a random shared gateway control secret:

```sh
pixi run python -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())'
pixi run python -c 'import secrets; print(secrets.token_hex(32))'
```

Capture these in your secret store/private provisioning session. Preserve the encryption key
across restarts. The same gateway control value goes into the scheduler's environment and the
gateway's service-owned credential file. No cloud API token substitutes for these credentials.

## 7. Render and install deployment bundles

Generate the gateway's dedicated SSH host key on the gateway after creating `/etc/fl`:

```sh
sudo ssh-keygen -t ed25519 -f /etc/fl/transfer-host-ed25519 -N ''
```

Do not overwrite an existing host key. Supply only the `ssh-ed25519 BASE64` public-key fields,
without a comment, in the deployment manifest. The private key remains on the gateway.

Copy `services/deployment/example.yaml` to your non-secret deployment inventory. Replace all
names, actual local interface addresses, allocated Headscale addresses, and the public host key.
Keep `license_relay: null` or omit it. Render to a new destination:

```sh
cd /opt/fl
pixi run fl-deploy /path/to/deployment.yaml --output /path/to/new-bundle
```

Review and distribute only each host's files. Installation destinations:

| Bundle files | Install destination |
| --- | --- |
| `scheduler/headscale.yaml`, `scheduler/policy.json` | `/etc/headscale/config.yaml`, `/etc/headscale/policy.json` |
| Scheduler/gateway `*.service` | `/etc/systemd/system/` |
| Scheduler nginx fragments | nginx `http` includes; Headscale map first |
| Gateway nginx fragment | nginx `http` includes on gateway |
| `gateway/{public,private}-endpoint.json` | `/etc/fl/{public,private}-endpoint.json` on scheduler |
| `gateway/sshd.conf` | `/etc/fl/transfer-sshd.conf` on gateway |
| Each host's `nginx.service.d/10-headscale.conf` | Matching directory under `/etc/systemd/system/` |
| `*/environment.defaults` | Merge into that host's protected environment file |

The endpoint JSON files must be readable by `fl-scheduler`; they pin the gateway host key.
Do not install duplicate bootstrap/final nginx server blocks. Replace the bootstrap fragment
with the final one, install valid certificates, reload Headscale's DNS/policy, and verify its
private addresses are present before nginx validation. Check live policy with:

```sh
sudo -u headscale headscale --config /etc/headscale/config.yaml policy check \
  -f /etc/headscale/policy.json
```

Full renderer details and native checks: [Linux deployment](linux-deployment.md).

## 8. Transfer gateway

Build/install the pinned BBCP tool on the gateway:

```sh
cd /opt/fl
pixi run python tests/build_bbcp.py --directory /tmp/fl-bbcp
sudo install -d -m 0755 /opt/fl-tools
sudo install -m 0755 /tmp/fl-bbcp/bbcp /opt/fl-tools/bbcp
```

Create `/var/lib/fl-transfer/control-secret`, owned by `fl-transfer`, mode `0600`, containing
the shared random secret. Set `/etc/fl/transfer-gateway.env` to the rendered defaults:

```ini
FL_GATEWAY_ROOT=/var/lib/fl-transfer
FL_GATEWAY_BBCP=/opt/fl-tools/bbcp
FL_GATEWAY_CONTROL_SECRET_FILE=/var/lib/fl-transfer/control-secret
FL_GATEWAY_DATA_PORT_FIRST=5000
FL_GATEWAY_DATA_PORT_LAST=5099
FL_BIND_HOST=127.0.0.1
FL_BIND_PORT=8081
```

Use your manifest's ports if different. Validate `/etc/fl/transfer-sshd.conf` with
`sudo /usr/sbin/sshd -t -f /etc/fl/transfer-sshd.conf`. The dedicated daemon uses port 22 on the
gateway's public-facing and Headscale addresses; move administrative SSH to a nonconflicting
port/address and verify that management access works before closing your existing session.

The repository supplies the API unit, not an installed SSH unit. An example operator-created
`/etc/systemd/system/fl-transfer-sshd.service` is:

```ini
[Unit]
Description=Fletcherlake dedicated transfer SSH daemon
After=network-online.target tailscaled.service fl-transfer-gateway.service
Wants=network-online.target tailscaled.service fl-transfer-gateway.service

[Service]
Type=simple
ExecStartPre=/usr/bin/install -d -m 0755 /run/sshd
ExecStartPre=/usr/sbin/sshd -t -f /etc/fl/transfer-sshd.conf
ExecStart=/usr/sbin/sshd -D -f /etc/fl/transfer-sshd.conf
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
```

Adjust executable paths for your distro. Keep this daemon privileged so it can authenticate
and switch to `fl-transfer`; the forced command itself runs as that account. Do not add ordinary
login keys to its generated authorized-keys file. Start after installing the final nginx files:

```sh
sudo systemd-analyze verify /etc/systemd/system/fl-transfer-gateway.service \
  /etc/systemd/system/fl-transfer-sshd.service
sudo nginx -t
sudo systemctl daemon-reload
sudo systemctl enable --now fl-transfer-gateway fl-transfer-sshd
sudo systemctl enable --now nginx
```

See [gateway operations](transfer-gateway.md) for scopes, integrity, revocation and retention.

## 9. PostgreSQL, scheduler and dashboard

Install/manage PostgreSQL separately; `pixi install` supplies development tooling, not a
production database service. The test environment covers PostgreSQL 17/18. For Ubuntu package
installation see [PostgreSQL's instructions](https://www.postgresql.org/download/linux/ubuntu/).
Create a dedicated login and database, for example from an administrator's `psql` session:

```sql
CREATE ROLE fl_scheduler LOGIN;
\password fl_scheduler
CREATE DATABASE fletcherlake OWNER fl_scheduler;
```

Configure password authentication and local/private binding in PostgreSQL. The role must be
able to apply migrations in its own database. If using RDS/Cloud SQL instead, supply the private
endpoint, required TLS settings and DB credentials in the URL; provider IAM authentication or
Cloud SQL proxy startup is not implemented by this application.

Create root-owned mode-0600 `/etc/fl/scheduler.env`, merging generated defaults with:

```ini
FL_DATABASE_URL="postgresql+psycopg://fl_scheduler:URL_ENCODED_PASSWORD@127.0.0.1:5432/fletcherlake"
FL_BIND_HOST=127.0.0.1
FL_BIND_PORT=8080
FL_PUBLIC_ORIGIN=https://scheduler.example.edu
FL_GOOGLE_CLIENT_ID=REPLACE_WITH_WEB_CLIENT_ID
FL_GOOGLE_CLIENT_SECRET=REPLACE_WITH_WEB_CLIENT_SECRET
FL_GOOGLE_GROUPS_BACKEND=cloud_identity
FL_GOOGLE_GROUPS_CREDENTIALS=/etc/fl/google-groups.json
FL_GOOGLE_WORKSPACE_DOMAIN=example.edu
FL_GOOGLE_USERS_GROUP=fl-users@example.edu
FL_GOOGLE_OPERATORS_GROUP=fl-operators@example.edu
FL_GOOGLE_ADMINS_GROUP=fl-admins@example.edu
FL_HEADSCALE_ADMIN_URL=http://127.0.0.1:8081
FL_HEADSCALE_LOGIN_URL=https://headscale.example.edu
FL_HEADSCALE_API_KEY=REPLACE_WITH_HEADSCALE_API_KEY
FL_ENROLLMENT_ENCRYPTION_KEY=REPLACE_WITH_PERSISTENT_FERNET_KEY
FL_AGENT_ORIGIN=https://agent.scheduler.example.edu
FL_TRANSFER_GATEWAY_ORIGIN=https://gateway.internal.example.edu
FL_TRANSFER_GATEWAY_CONTROL_SECRET=REPLACE_WITH_SHARED_GATEWAY_SECRET
FL_TRANSFER_PRIVATE_ENDPOINT_FILE=/etc/fl/private-endpoint.json
FL_TRANSFER_PUBLIC_ENDPOINT_FILE=/etc/fl/public-endpoint.json
FL_TRANSFER_PUBLIC_ORIGIN=https://transfer.example.edu
FL_DASHBOARD_DIR=/opt/fl/services/dashboard/dist
```

URL-encode special characters in database passwords. This is a systemd environment file; do
not blindly source it as a shell script. Install the Groups JSON key with the ownership/mode
from section 3. Build the optional dashboard from the checkout:

```sh
cd /opt/fl
pixi run -e web web-install
pixi run -e web web-check
pixi run -e web web-test
pixi run -e web web-build
```

Node is only needed for the build. If omitting the dashboard, remove `FL_DASHBOARD_DIR` from
the environment; the rendered defaults enable it and startup checks for a real build.
Run migrations with systemd loading the same protected environment:

```sh
sudo systemd-run --wait --pipe --collect \
  --property=User=fl-scheduler --property=WorkingDirectory=/opt/fl \
  --property=EnvironmentFile=/etc/fl/scheduler.env \
  /opt/fl/.pixi/envs/default/bin/alembic \
  -c /opt/fl/services/scheduler/alembic.ini upgrade head
```

Then validate and start:

```sh
sudo systemd-analyze verify /etc/systemd/system/fl-scheduler.service
sudo nginx -t
sudo systemctl daemon-reload
sudo systemctl enable --now fl-scheduler
sudo systemctl reload nginx
curl --fail https://scheduler.example.edu/healthz
```

Sign into `/clusters` with the initial administrator and check `/admin/clusters` and
`/admin/users`. Notifications, placement, enrollment cleanup and transfer/export workers run
in the scheduler process; no separate worker service is needed. See
[scheduler operations](scheduler-operations.md) and [dashboard operations](scheduler-dashboard.md).

## 10. Mac agents, firmware and local interfaces

Install the same checkout at `/opt/fl`, Pixi, an active Tailscale client, Apple's command-line
tools, OpenSSL and BBCP. The pinned BBCP helper has a native Mac build path:

```sh
cd /opt/fl
pixi install --locked
pixi run python tests/build_bbcp.py --directory /tmp/fl-bbcp \
  --openssl-prefix /opt/homebrew/opt/openssl@3
```

Install its binary in an administrator-owned stable location. Explicitly set
`environment.bbcp.path` if it is not in launchd's PATH.

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
  --scheduler https://scheduler.example.edu
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

On an ordinary Linux/Chipyard or Mac user host, install the checkout's locked environment,
OpenSSH and BBCP. A user can build BBCP with the same helper and put it on their own PATH;
root and Headscale membership are not needed. From that checkout:

```sh
pixi install --locked
pixi run fl-client login --scheduler https://scheduler.example.edu
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

Rotate OAuth/service-account keys, Headscale API keys, cloud identities and provider secrets
through their own providers, updating protected files and restarting affected services. Preserve
the enrollment encryption key unless deliberately handling outstanding receipts. Gateway host
key changes require regenerated/pinned endpoint metadata; do not bypass host-key checking.

For upgrades, deploy the same reviewed revision, stop old scheduler instances before migrations,
apply Alembic explicitly, rebuild the dashboard, and restart. Agents keep local authority through
scheduler outages. Reconfigure Macs through the management workflow. Retired Macs still need
their separate launchd retention job until collateral expires; see [retirement](retirement.md).
