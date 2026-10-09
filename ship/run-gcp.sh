source ./env-vars.sh

source ./helpers.sh

./create-gcp-vpcs.sh

./create-gcp-firewall-launch-inst.sh # admin ssh key stored at $HOME/.local/state/fl-deploy/admin-ed25519 - both for fl-scheduler (:22) and fl-transfer-gateway (initially :22, then :2222)

./create-ggroups-access-service-acct.sh

# go make your oauth web client - set callback as: https://scheduler.<your-FL_DOMAIN>/api/auth/callback

./record-g-oauth-credentials.sh # will need to wait for user input here

gcloud services enable cloudidentity.googleapis.com --project="$FL_GOOGLE_PROJECT" # https://github.com/jimfangx/fletcherlake-scheduler/blob/main/docs/deployment-guide.md#group-owner-service-account-no-domain-wide-delegation

# should make 3 google groups, all of them have the iam.gserviceaccount.com as the owner.
# |  |  |
# |---|---|
# | `fl-users@<workspace-domain>` | Submit jobs, view their jobs, and view clusters |
# | `fl-operators@<workspace-domain>` | User permissions plus manage jobs and drain, restart, or power off clusters |
# | `fl-admins@<workspace-domain>` | Operator permissions plus enroll/remove clusters and administer the system |
# - Add **your own Google account directly to `fl-admins`** to start with
export FL_GOOGLE_GROUPS_BACKEND=cloud_identity
# export FL_GOOGLE_GROUPS_CREDENTIALS=/etc/fl/google-groups.json # TODO: SET THIS TO THE SERVICE ACCOUNT OWNING THE ACCESS WHITELIST GOOGLE GROUP

./dns.sh # will wait for user input, My Profile → API Tokens → Create Token, create a token with Zone → DNS → Edit and Zone → Zone → Read, restricted to Include → Specific zone → your authoritative zone.

./tls.sh # sets up certs + autorenewal for our 2 vms that host sites (scheduler + gateway) -- configs are uploaded automatically

./copy-source-to-vms.sh

./setup-vm-disk.sh

./change-gateway-admin-ssh-port.sh # this is to change administrative ssh to 2222 + restrict to the administrator's public IP set in env-vars.sh; this si because rclone uses :22 for SFTP transfer. this is only for the gateway, scheduler + dashbaord can keep :22 + restricted to admin's public IP. When running this, do this in another terminal so you dont lose access to the machine if it fails

./bootstrap-headscale.sh # **starts Headscale on the scheduler and starts `tailscaled` on both VMs**. Starting `tailscaled` does **not** enroll either VM into your Headscale network.

./vms-join-headscale.sh # make both manager & gateway instances join headscale; Then: a) - **Headscale API key:** Lets the scheduler manage enrollment through Headscale’s API; expires after 720 hours. b) - **Gateway control secret:** Authenticates scheduler-to-gateway control requests. for file transfers... c) - **Postgres password:** Authenticates database access on scheduler. d) - **Enrollment encryption key:** Encrypts enrollment credentials saved by the scheduler.

./prepare-gateway-deployment-profile.sh # preprare the manifest of the transfer gateway on the scheduler.

./gateway-up.sh # transfer manifest to gateway, open public transfer on port 22

# change firewall of gateway to allow :22 open to public for transfers -- transfers require an authorized ssh key; password login is disabled, only the fl-transfer acct is allowed. The scheduler registers keys scoped to a specific transfer. Grants expire within 10 minutes and can be revoked.
# Each key runs a fixed transfer handler. Shell commands, terminals, and SSH forwarding are disabled.
# &#32;The handler exposes only the files listed in that transfer’s manifest, with the permitted upload/download direction. It blocks arbitrary filesystem paths.
# Uploaded files must match the declared size and SHA-256 hash.
gcloud compute firewall-rules create fl-gateway-transfer --project="$FL_GCP_PROJECT" \
  --network=fl-vpc --direction=INGRESS --priority=1000 --action=ALLOW \
  --target-tags=fl-transfer-gateway --source-ranges=0.0.0.0/0 --rules=tcp:22
gcloud compute firewall-rules update fl-bootstrap-admin --project="$FL_GCP_PROJECT" \
  --target-tags=fl-scheduler


./deploy-dashboard-and-db.sh # at this point, sign in to https://scheduler.fl.ucb.bar/clusters and check /admin/clusters & /admi/users

export FL_MAC_SSH=REPLACE_WITH_ADMIN_USER@REPLACE_WITH_MAC_HOST # must be reachable from the deployment server

./bringup-mac.sh

./run-on-new-mac.sh

./init-registration-new-mac-with-scheduler.sh # run on mac

