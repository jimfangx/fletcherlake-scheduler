IFS= read -r -s -p 'Cloudflare zone-scoped API token: ' FL_INPUT
printf '\n'
if [[ -z "$FL_INPUT" || "$FL_INPUT" == *[[:space:]]* ]]; then
  unset FL_INPUT
  printf 'Invalid Cloudflare token: paste only the API token value, without spaces or a Bearer prefix. Saved credentials were not changed.\n' >&2
  return 1 2>/dev/null || exit 1
fi
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
if not token or any(c.isspace() for c in token):
    raise ValueError('Cloudflare token is empty or contains whitespace; paste only the API token value')
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
