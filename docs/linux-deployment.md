# Render Linux deployment files

The scheduler and Headscale share one Linux host. The transfer gateway uses a separate host.
An optional license relay uses a BWRC-connected host only when forwarding is needed.
Mac agents continue using launchd; the Linux units below
do not apply to them. See [Mac operations](macos-operations.md).

For GCP VM sizes, NIC/address mapping, HTTP/HTTPS settings and per-role firewall rules,
follow [the GCP deployment plan](deployment-guide.md#gcp-nic-and-vm-creation-settings).

Copy `services/deployment/example.yaml` into your deployment inventory. Replace the example
addresses and hostnames with the hosts' actual interface addresses and DNS names. Public
addresses must differ from allocated Headscale addresses. Cloud hosts behind NAT should use
their local interface address for the public listener, with the public DNS/firewall mapping
managed separately. Supply the gateway daemon's canonical Ed25519 **public** host key without
a comment. The example's key placeholder intentionally fails validation.

For the current Vivado Lab setup, omit `license_relay` or leave it as `null`, as in the example.
The renderer then emits no relay files, HAProxy drop-in, license DNS record or license ACL/tag.
There is no BWRC host, credential or checkout prerequisite for that deployment.
If forwarding is needed later, supply a complete `license_relay` mapping as documented in
[license relay operations](license-relay.md). Its backend must remain the actual BWRC server,
not the relay's address. No subnet or exit route is created.

From the repository checkout, run:

```sh
pixi run fl-deploy /PATH/deployment.yaml --output /PATH/new-review-bundle
```

The destination must be absent and its parent must exist. Rendering creates protected files
and directories, refuses overwrite, and leaves installed services alone. `--templates /PATH/services`
selects templates from another reviewed checkout. Unknown fields, secret fields, wildcard
addresses, duplicate service names, unsafe paths and invalid keys are rejected. Ordinary spaces,
quotes and percent signs in installation paths are supported. Paths containing controls,
variables, backslashes, `..` or trailing whitespace are rejected. Validation failures report
field locations and error types without echoing supplied values or malformed YAML contents.

The bundle freezes the validated manifest and provides these files:

| Host | Files and destination |
| --- | --- |
| Scheduler | `headscale.yaml` → `/etc/headscale/config.yaml`; `policy.json` → `/etc/headscale/policy.json`; both nginx fragments inside nginx's `http` block, Headscale first; `headscale.service` and `fl-scheduler.service` → `/etc/systemd/system` |
| Scheduler | Both `gateway/*-endpoint.json` files → `/etc/fl/{private,public}-endpoint.json`; merge `scheduler/environment.defaults` into `/etc/fl/scheduler.env` |
| Gateway | `transfer-gateway-nginx.conf` inside nginx's `http` block; `sshd.conf` → `/etc/fl/transfer-sshd.conf`; `fl-transfer-gateway.service` → `/etc/systemd/system`; merge `gateway/environment.defaults` into `/etc/fl/transfer-gateway.env` |
| License relay (when enabled) | `haproxy.cfg` → the distro HAProxy service's configuration path |
| Enabled hosts | The host's `*.service.d/10-headscale.conf` → `/etc/systemd/system` for nginx or HAProxy |

The service defaults contain no secrets and are incomplete until the operator supplies protected
Google/Directory, database, Headscale, enrollment and gateway credentials. Follow
[scheduler operations](scheduler-operations.md), [authentication](authentication.md) and
[gateway operations](transfer-gateway.md). The scheduler's control credential must match the
gateway's protected credential file. Keep keys out of this manifest and out of cluster/job YAML.
Build the dashboard before enabling its rendered `FL_DASHBOARD_DIR`.

The renderer updates the gateway endpoint metadata, SSH-only ACLs and gateway environment
defaults together. When enabled, it also updates license frontends, backends, ACL ports and private DNS together,
including swapped manager/vendor ports. Preserve the fixed port pair. Both gateway endpoints
pin the same supplied SSH host key; never accept an unknown key automatically.

Install certificates at the rendered paths, using DNS validation for private names. Keep
application/tool installations administrator-owned. Create the service accounts and state roots
described in the role guides. Apply PostgreSQL migrations before starting the scheduler. Manage
the dedicated transfer SSH daemon separately from administrative SSH, with nonconflicting
explicit address bindings. Configure public DNS and host/cloud firewalls separately; configure
BWRC egress only when the license relay is enabled.
Public transfer SSH/SFTP on TCP 22 is the scoped data-plane exception to public HTTPS-only control.

Validate on each deployment host before loading files:

```sh
nginx -t
sshd -t -f /etc/fl/transfer-sshd.conf
haproxy -c -f /PATH/haproxy.cfg
headscale --config /etc/headscale/config.yaml configtest
systemd-analyze verify /etc/systemd/system/fl-scheduler.service
```

Run only that host's checks, including its other installed units. nginx validation can open its
configured listeners, so Headscale addresses and certificates must already be available. The
drop-ins order startup after tailscaled and retry failures every five seconds while an address
is unavailable. This preserves explicit bindings; it never changes a listener to a wildcard.
After review and installation, reload systemd and start the host's services using your normal
deployment procedure. Verify service health, TLS names, private agent reachability and firewall
denials before enrolling production Macs.

Headscale 0.29's policy checker resolves live users and nodes. After starting the coordinator,
run `headscale --config /etc/headscale/config.yaml policy check -f /etc/headscale/policy.json`.
For offline validation with the coordinator stopped, add
`--bypass-grpc-and-access-database-directly` and answer its confirmation. Native acceptance
uses this offline path with a new temporary database and an explicit configuration file.

## Reproduce native acceptance on Linux

Python/macOS dependencies remain in the default environment. nginx is locked in a separate,
Linux-only `deployment` environment; Node remains in the separate `web` environment.

```sh
pixi install --locked -e deployment
pixi run python tests/download_network_tools.py --directory /tmp/fl-network
pixi run python tests/build_haproxy.py --directory /tmp/fl-haproxy
FL_TEST_NGINX="$PWD/.pixi/envs/deployment/bin/nginx" \
FL_TEST_HAPROXY=/tmp/fl-haproxy/haproxy \
FL_TEST_HEADSCALE=/tmp/fl-network/headscale \
pixi run pytest -q tests/test_deployment_cli.py tests/test_deployment_render.py \
  tests/test_deployment_native.py \
  tests/test_deployment_https.py tests/test_deployment_relay.py
```

Install a C compiler/make, OpenSSH server and systemd analysis tools first. The HAProxy builder
downloads official 3.4.6 source and verifies its pinned SHA256 before compilation. Linux CI
enables these checks; missing tools skip native acceptance only when `FL_TEST_NGINX` is unset.
An explicitly configured but missing tool fails.

Acceptance checks actual nginx, HAProxy, sshd, Headscale and systemd parsers, including paths with
spaces/quotes/percent signs and Headscale policy parsing. Standard systemd dependencies and
distro proxy base units are isolated parser stubs; this is not a systemd boot test. Native runtime
fixtures map each production listener to a distinct loopback alias and unprivileged port. They
start actual TLS nginx and a TCP HAProxy relay, verify public/private route isolation, spoofed
forwarded-header replacement, private WebSocket upgrades, 429 rate limits, 413 body limits, the
larger unregister body allowance, OAuth query-log exclusion, and both fixed private relay ports
with denials on another interface/port. They make no BWRC or provider calls.

These checks do not establish production service boot, public certificate issuance, operational
DERP fallback, native Mac DNS/TLS or an actual license checkout/release for an enabled relay.
Those remain explicit
gates in [the requirement ledger](implementation-status.md).
