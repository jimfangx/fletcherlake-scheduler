gcloud compute networks create fl-vpc --subnet-mode=custom
gcloud compute networks subnets create fl-subnet \
  --network=fl-vpc --region="$FL_GCP_REGION" --range="$FL_SUBNET_CIDR"
gcloud compute addresses create fl-scheduler-internal \
  --region="$FL_GCP_REGION" --subnet=fl-subnet --addresses="$FL_SCHEDULER_NIC_IP"
gcloud compute addresses create fl-gateway-internal \
  --region="$FL_GCP_REGION" --subnet=fl-subnet --addresses="$FL_GATEWAY_NIC_IP"
