# Private infrastructure network

The scheduler owns enrollment; Headscale owns node identity and packet policy. User machines
stay outside the private network. Production uses Headscale **0.29.4** with the matching
[versioned configuration reference](https://github.com/juanfont/headscale/blob/v0.29.4/config-example.yaml).
The REST adapter is checked against its
[versioned API contract](https://github.com/juanfont/headscale/blob/v0.29.4/gen/openapiv2/headscale/v1/headscale.swagger.json).
Use the [Linux deployment renderer](linux-deployment.md) to keep hostnames, explicit listener
addresses, private DNS, license ports and transfer ACLs consistent before installing templates.
See [combined private-network acceptance](private-network-acceptance.md) for actual private
HTTPS/WSS, rclone/SFTP payloads and assigned-node SSH source fencing with userspace peers.

Install the official Headscale release and verify its published checksum before execution.
Create an unprivileged `headscale` service account and install
`services/headscale/config.yaml` and `policy.json` into `/etc/headscale`. Use root ownership,
group `headscale`, directory mode 0750, and file mode 0640. Replace the public hostname in
the YAML. Install `services/headscale/headscale.service` into `/etc/systemd/system` and start
it. Its SQLite database and Noise key live in `/var/lib/headscale`, mode 0700. Back up both
together with the policy and configuration. Do not replace the scheduler's PostgreSQL database
with this SQLite database.

For an existing installation, preserve those backups and follow the release's upgrade guide.
Upgrade one minor version at a time; an installation on 0.28 can move to 0.29.4, while older
databases need their intermediate upgrades first. The shipped configuration uses the 0.29
`node.ephemeral.inactivity_timeout` setting. Infrastructure keys remain non-ephemeral and tagged
node identities remain persistent. Do not replace keys or re-enroll nodes to work around a
policy update failure.

Install Tailscale 1.80.0 or newer on infrastructure hosts, matching the coordinator's
[supported client floor](https://github.com/juanfont/headscale/releases/tag/v0.29.4).
CI checks Tailscale 1.102.4. On the coordinator, create a separate, nonreusable,
ten-minute key for the scheduler:

```sh
sudo -u headscale headscale --config /etc/headscale/config.yaml preauthkeys create \
  --tags tag:scheduler --expiration 10m
```

Deliver its secret through a protected key file to `tailscale up --auth-key file:/PATH`, using
the public Headscale login URL. Never put the secret directly in process arguments. Repeat
with `tag:transfer-gateway` on the gateway. Issue a `tag:license-relay` key only when the optional
license relay is enabled in the deployment inventory; Vivado Lab deployments omit it.
These tagged infrastructure keys do not require a human Headscale user. Human authorization
remains in Google Groups. Do not advertise subnet routes or an exit node.

The shipped policy permits only the listed TCP flows. Tag owners are empty; tags are assigned
by administrator-issued keys. Cluster nodes cannot contact one another. Scheduler/API ports
are 443; the gateway SSH/SFTP ports are reserved for the transfer implementation. Keep its
configured data range and the ACL consistent when that implementation is deployed.

Obtain public HTTPS certificates for the scheduler and coordinator. Obtain the private agent
hostname's certificate through DNS validation. Configure nginx using
`services/headscale/nginx.conf` and `services/scheduler/nginx.conf`, replacing every placeholder
and verifying the rendered configuration with `nginx -t` before loading it. Bind the public
listeners to the public host address and the agent listener to the scheduler's actual
Headscale address. Ensure the private address is available when nginx starts. The templates
preserve the control protocol Upgrade header and long-lived connections; see the
[Headscale proxy reference](https://headscale.net/stable/ref/integration/reverse-proxy/).

Add a private DNS record under `dns` in Headscale's configuration:

```yaml
extra_records:
  - name: agent.scheduler.example.edu
    type: A
    value: 100.64.0.1 # Replace with this scheduler node's allocated address.
```

Reload/restart Headscale after editing the policy or DNS. Enrolled Macs accept its DNS
configuration. Public DNS for the bootstrap scheduler remains publicly reachable before a
Mac joins. Public nginx rejects `/api/agents/`; the Headscale listener exposes those routes.
The Headscale admin REST listener, metrics, and gRPC remain on loopback. Public nginx rejects
its `/api/` routes. Do not expose agent listeners, PostgreSQL, or the Headscale Unix socket
on the public host address.

Create a Headscale administrative API key on the coordinator, set an explicit expiration,
and inject it into the scheduler's protected environment file. Rotate it before expiry.
For protected AWS/GCP commands, see
[API-key rotation](deployment-guide.md#rotating-the-schedulers-headscale-api-key). The scheduler
loads the replacement on restart; existing Mac node identities remain enrolled.
The Mac never receives that credential. Also generate a Fernet key for the scheduler:

```sh
pixi run python -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())'
```

Set `FL_HEADSCALE_ADMIN_URL=http://127.0.0.1:8081`, `FL_HEADSCALE_LOGIN_URL` to the public HTTPS
coordinator origin, `FL_AGENT_ORIGIN` to the private HTTPS agent origin,
`FL_HEADSCALE_API_KEY`, and `FL_ENROLLMENT_ENCRYPTION_KEY`. Retain the encryption key across
scheduler restarts; changing it makes outstanding delivery receipts unreadable. Upgrade
Alembic through the current head (`0010_notification_delivery`) before restarting the scheduler; enrollment
tables were introduced in `0006_enrollment`.

## Mac enrollment and cleanup

An authenticated administrator issues a ticket with `POST /api/admin/enrollments`, optionally
setting `lifetime_seconds` between 60 and 3600 (default 1800). Its response contains a stable
cluster UUID and a one-time bootstrap token. Inventory listings omit secrets. Administrators
can revoke unconsumed tickets with `DELETE /api/admin/enrollments/{enrollment_id}`.
The administrator dashboard also supports ticket issuance and revocation; its visible secret
is cleared on navigation. The authenticated REST API exposes the same enrollment operations.

Run `fl cluster setup init INVENTORY.yaml --scheduler https://scheduler.example.edu` on the
Mac and enter the token at the hidden prompt. Setup saves protected retry credentials before
claiming the ticket, obtains a nonreusable join key expiring within ten minutes, joins
Headscale, and registers the verified node. Registration checks its key provenance, exact
cluster tag, private addresses, and stable UUID. The bootstrap, human, agent, and Headscale
administrative credentials have distinct scopes.

Only enrollment credentials are temporary. Infrastructure nodes are deliberately persistent
(`ephemeral: false`), so a reboot or a network outage cannot silently remove their identity.
Short-lived keys are single-purpose and nonreusable. After registration, the encrypted key
delivery receipt is removed. Config YAML never contains tokens. Protected `credentials.json`
contains the private agent origin, public unregister origin, and agent token.

Setup can retry a lost response with its original inventory and token. Its protected
`enrollment.json` binds retries to the same bootstrap and cluster UUID; an OS lock prevents
concurrent setup from replacing that receipt. A lost registration reply replays the committed
receipt and never issues another key or changes identity. Expired join keys require retiring
the pending ticket and starting a new enrollment. Do not remove the receipt while its node
is still registered; revoke the ticket first so cleanup removes the abandoned node.

Destroy drains local hardware, interrupts active jobs, cancels queued work, finalizes retention,
and sends the final snapshot to the scoped public HTTPS unregister API. The scheduler commits
job/artifact state, fences the WebSocket session, disables inventory, and queues node deletion
atomically.
Provider failures retry from PostgreSQL after restart. Revoked/expired unused tickets also
expire their keys and remove any nodes they created, including delayed joins. Network cleanup
never runs firmware operations. Local collateral remains governed by the stored retention
timestamps. A separate macOS launchd maintenance job continues cleanup of protected archives
after agent removal. The public unregister action remains reachable after node revocation if
its response was lost; other agent control endpoints remain private. See
[permanent retirement](retirement.md) for retry and re-enrollment behavior.

## Verification

The opt-in acceptance tests use three isolated userspace Tailscale peers and a real disposable
Headscale process. They verify adapter node metadata, a private cluster→scheduler response,
scheduler→cluster access, denied cluster→cluster access, and a denied scheduler port. They
need sockets, but neither root nor a TUN device. The fixture runs a real local embedded DERP
server. Combined transfer/terminal scenarios also disable UDP on every peer and require relay
traffic counters. That establishes disposable Linux relay transport; production HTTPS DERP,
real NAT/firewall traversal and native Mac networking remain operational acceptance gates.

The relay-only restart scenario also stops and restarts the coordinator with its existing
database and keys while the scheduler and peer daemons remain running. It verifies preserved
node identities, uninterrupted local execution, offline completion and queued cancellation
reconciliation. Internal DNS answers remain available from the peers' cached network maps
during the outage; this does not verify macOS resolver installation. See
[private-network acceptance](private-network-acceptance.md) for the command and report.

```sh
pixi run python tests/download_network_tools.py --directory /tmp/fl-network
FL_TEST_HEADSCALE=/tmp/fl-network/headscale \
FL_TEST_TAILSCALE_DIR=/tmp/fl-network/tailscale_1.102.4_amd64 pixi run test
```

The downloader pins official Headscale 0.29.4 and Tailscale 1.102.4 Linux amd64 SHA256 values
and authenticates bytes before extraction/execution. Linux CI enables these checks. On macOS,
the injected setup/launchd tests run, while real installation, DNS, TLS, reboot, and firmware
checks still require a Mac. The production systemd/nginx templates need rendering and live
verification on the deployment host; the tests do not install them.
