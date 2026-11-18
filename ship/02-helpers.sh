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
