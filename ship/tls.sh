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
