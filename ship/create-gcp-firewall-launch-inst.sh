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
