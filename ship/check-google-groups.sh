#!/usr/bin/env bash
# Source from the operator Bash shell after loading settings, resources and helpers.
if [ -z "${BASH_VERSION:-}" ]; then
  printf 'Run this diagnostic from the deployment Bash shell.\n' >&2
  return 1 2>/dev/null || exit 1
fi
FL_CHECK_EMAIL=${1:?Usage: source ship/check-google-groups.sh YOUR_GOOGLE_EMAIL}
printf -v FL_CHECK_EMAIL_ARG '%q' "$FL_CHECK_EMAIL"
flssh scheduler "bash -se -- $FL_CHECK_EMAIL_ARG" <<'HOST'
FL_CHECK_EMAIL=$1
FL_CHECK_SCRIPT=$(sudo mktemp /run/fl-google-groups-check.XXXXXX.py)
trap 'sudo rm -f "$FL_CHECK_SCRIPT"' EXIT
sudo tee "$FL_CHECK_SCRIPT" >/dev/null <<'PY'
import asyncio
import os
import sys

import httpx
from fl_common.errors import PlatformError
from fl_scheduler.auth.group_directory import load_group_directory
from google.auth.exceptions import RefreshError


async def main():
    backend = os.environ.get('FL_GOOGLE_GROUPS_BACKEND', 'directory')
    print(f'Groups backend: {backend}')
    print(f'Checking human membership: {sys.argv[1]}')
    failed = False
    async with httpx.AsyncClient(timeout=15) as client:
        try:
            directory = load_group_directory(client)
        except Exception as error:
            print(f'Credential/configuration loading failed: {type(error).__name__}')
            print('Check credential path, ownership/mode and configured backend.')
            return 1
        credentials = getattr(directory, 'credentials', None)
        account = getattr(credentials, 'service_account_email', None)
        if account:
            print(f'Group-reader identity: {account}')
        for variable in ('FL_GOOGLE_ADMINS_GROUP', 'FL_GOOGLE_OPERATORS_GROUP', 'FL_GOOGLE_USERS_GROUP'):
            group = os.environ.get(variable)
            if not group:
                print(f'{variable}: not configured')
                continue
            try:
                member = await directory.has_member(group, sys.argv[1])
                print(f'{variable}={group}: member={member}')
            except PlatformError as error:
                failed = True
                cause = error.__cause__
                print(f'{variable}={group}: lookup failed ({type(cause).__name__})')
                if isinstance(cause, httpx.HTTPStatusError):
                    response = cause.response
                    # Do not print request headers, tokens, credentials or whole responses.
                    print(f'  API path: {response.request.url.path}; HTTP {response.status_code}')
                    try:
                        detail = response.json().get('error', {})
                        print(f"  Google status: {detail.get('status', 'unknown')}")
                        print(f"  Google message: {detail.get('message', 'not supplied')}")
                    except (ValueError, AttributeError):
                        print('  Google returned a non-JSON error.')
                elif isinstance(cause, RefreshError):
                    print('  Service-account token refresh failed; check key validity and VM time.')
                elif isinstance(cause, ValueError):
                    print('  Google returned membership data the authorization adapter rejected.')
    return 1 if failed else 0


raise SystemExit(asyncio.run(main()))
PY
sudo chmod 644 "$FL_CHECK_SCRIPT"
sudo systemd-run --wait --pipe --collect \
  --property=User=fl-scheduler --property=WorkingDirectory=/opt/fl \
  --property=EnvironmentFile=/etc/fl/scheduler.env \
  /opt/fl/.pixi/envs/default/bin/python "$FL_CHECK_SCRIPT" "$FL_CHECK_EMAIL"
HOST
