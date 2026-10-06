# Linux scheduler service

The `fl-scheduler` entrypoint composes human REST APIs, Google authentication, agent WebSockets,
and a five-second maintenance loop. Maintenance tracks heartbeat health, reserves eligible
jobs, automatically enqueues input-free jobs, deletes expired login/session state, and expires
short-lived live-log read commands and cached bytes.
Execution stays on agents. An independent delivery
worker resumes verified gateway-to-Mac transfers from PostgreSQL; a reservation alone does not
enqueue work. Mac verification ACKs gate durable enqueue commands.
An independent worker expires enrollment tickets and retries Headscale key/node revocations
from PostgreSQL. Cancellation reaps its current database thread before closing the pool.
Upload cleanup retries revocation for closed jobs and expired gateway retention, including
registrations that were in flight when cancellation or scheduler failure occurred.
An artifact export worker requests retained terminal files from Macs, verifies gateway copies,
and revokes exports/read scopes after deletion or expiry. All workers resume from PostgreSQL.
When configured, the notification consumer projects events into durable destination-scoped rows
and retries delivery with fenced leases and shared provider pacing. Maintenance also produces
bounded retention warnings; Mac execution warning events replay over the existing connection.

Run PostgreSQL locally or on a protected database network. Create a dedicated database and
service user, then inject these variables through a mode-0600 service environment file or your
secret manager:

| Variable | Meaning |
| --- | --- |
| `FL_DATABASE_URL` | SQLAlchemy PostgreSQL connection URL |
| `FL_PUBLIC_ORIGIN` | Public HTTPS origin without a path/trailing slash |
| `FL_GOOGLE_CLIENT_ID` | Google web OAuth client ID |
| `FL_GOOGLE_CLIENT_SECRET` | OAuth client secret |
| `FL_GOOGLE_DIRECTORY_CREDENTIALS` | Mode-0600 service-account JSON file |
| `FL_GOOGLE_DELEGATED_ADMIN` | Workspace administrator identity for delegation |
| `FL_GOOGLE_WORKSPACE_DOMAIN` | Optional enforced ID-token hosted domain |
| `FL_GOOGLE_USERS_GROUP` | Required authorized user group |
| `FL_GOOGLE_OPERATORS_GROUP` | Optional operator group |
| `FL_GOOGLE_ADMINS_GROUP` | Optional administrator group |
| `FL_HEADSCALE_ADMIN_URL` | Loopback HTTP admin origin, normally `http://127.0.0.1:8081` |
| `FL_HEADSCALE_LOGIN_URL` | Public HTTPS Headscale coordinator origin |
| `FL_HEADSCALE_API_KEY` | Headscale administrative API secret; never delivered to Macs |
| `FL_ENROLLMENT_ENCRYPTION_KEY` | Persistent Fernet key protecting temporary delivery receipts |
| `FL_AGENT_ORIGIN` | Private HTTPS agent origin reachable through Headscale |
| `FL_TRANSFER_GATEWAY_ORIGIN` | Private HTTPS gateway control origin |
| `FL_TRANSFER_GATEWAY_CONTROL_SECRET` | Gateway's distinct scheduler control credential |
| `FL_TRANSFER_PRIVATE_ENDPOINT_FILE` | Trusted JSON `TransferEndpoint` for the private BBCP listener and pinned host key |
| `FL_TRANSFER_PUBLIC_ENDPOINT_FILE` | Trusted JSON `TransferEndpoint` for the public BBCP listener and pinned host key |
| `FL_TRANSFER_PUBLIC_ORIGIN` | Public HTTPS gateway verification origin |
| `FL_NOTIFICATION_CONFIG_FILE` | Optional protected, service-owned JSON with Mailgun/webhook configuration |
| `FL_DASHBOARD_DIR` | Optional absolute path to the built dashboard `dist` directory |
| `FL_BIND_HOST` / `FL_BIND_PORT` | Defaults: `127.0.0.1` / `8080` |

Register the exact redirect URL `https://YOUR_HOST/api/auth/callback` in Google's OAuth client.
Authorize the service account through Workspace domain-wide delegation for the scope
`https://www.googleapis.com/auth/admin.directory.group.member.readonly`, and enable the
Admin SDK Directory API. The delegated administrator must be able to read group membership.
See [Google's credential guidance](https://developers.google.com/workspace/guides/create-credentials),
[ID-token verification](https://developers.google.com/identity/gsi/web/guides/verify-google-id-token),
and the [membership endpoint](https://developers.google.com/workspace/admin/directory/reference/rest/v1/members/hasMember).

Install the locked environment and upgrade the database before starting the service:

```sh
pixi install --locked
pixi run alembic -c services/scheduler/alembic.ini upgrade head
pixi run fl-scheduler
```

Build the React dashboard separately and set `FL_DASHBOARD_DIR` before service startup to serve
it on the public scheduler origin. See [dashboard operation](scheduler-dashboard.md) for locked
build commands and browser acceptance. Node belongs to the separate `web` Pixi environment;
neither Mac agents nor the Linux Python service need Node at runtime.

Startup rejects an outdated schema rather than implicitly migrating it. Serve public HTTPS on
443 through a reverse proxy to the loopback listener. Stop older scheduler instances before
applying revision 0010, then start the matching new binary: its event ingestion column is required
for writes. Agents keep executing and replay network receipts after the scheduler returns.
Use a separate Headscale-only HTTPS
listener for `/api/agents/`; its origin is delivered during enrollment. Proxy WebSocket upgrade headers
and allow long-lived connections. Do not expose port 8080, PostgreSQL, or agent Unix sockets
publicly. Configure `/healthz` as the database liveness check. It returns no inventory.
See [Headscale operations](headscale-operations.md) for config/ACLs, protected issuance and
cleanup, and [the optional license relay](license-relay.md). Vivado Lab deployments do not
require BWRC license access. Systemd and nginx templates are supplied in
`services/scheduler` and `services/headscale`; render and verify them on the deployment host.
See [transfer gateway operations](transfer-gateway.md) for the control credential and listener
configuration. Production startup requires Alembic revision `0010_notification_delivery`.

REST documentation is at `/docs`. API roots include `/api/auth`, `/api/jobs`, `/api/clusters`,
`/api/boards`, and `/api/admin/users`. Submit JSON containing canonical `config` plus optional
binary/bitstream content references. Owner, UUID and submission time come from the verified
principal and scheduler. Cancellation and drain return 202 because hardware effects require
an agent receipt/snapshot. Deletion is forbidden for active jobs. Cancellation fences enqueue
under the same PostgreSQL advisory lock used by reservations; delayed staging snapshots cannot
revive a job canceled before enqueue. A pending drain blocks placement while the physical
cluster continues reporting READY, and clears only after an authoritative drained snapshot.

WebSocket command replay uses bounded 32-command batches under the agent's 64-command dispatch
capacity. Cancellation can pass long transfers once local job creation is acknowledged; staging
and enqueue dependencies remain ordered. See [load and partition acceptance](scheduler-acceptance.md).

`POST /api/jobs/<UUID>/delivery` accepts only an `upload_id`. Owner or operator/admin authorization
runs before the private gateway is queried. The gateway must attest VERIFIED bytes matching the
job's complete input manifest. The scheduler then freezes the assignment and persists JOB_STAGE
with a unique download identity scope. The Mac returns its scoped public key, receives JOB_FETCH,
pulls through Headscale, and verifies SHA before ACK. Only then does the scheduler issue
JOB_ENQUEUE, which verifies the local inputs again. API replay and worker restart reuse the same
delivery. Transient fetch failures use new message IDs with bounded retries and a ten-minute
delivery deadline. Cancellation or permanent integrity failure prevents enqueue and revokes
the private download scope. A superseded WebSocket session cannot acknowledge commands.

`POST /api/submissions` accepts an immutable request UUID, canonical config/content references,
and a public SSH identity with staging-token hash when inputs exist. A PostgreSQL transaction
binds these to the verified owner and a server-assigned job UUID. Concurrent/repeated identical
requests reuse that job; changed metadata returns SUBMISSION_ID_CONFLICT. Private identities
and raw staging tokens stay on the user host.

`POST /api/jobs/<UUID>/upload` authorizes the owner and returns WAITING until a live reservation
exists. An UPLOAD ticket contains the public endpoint, verification origin, and persisted scope.
Credentials expire within ten minutes and cannot outlast the current reservation. The immutable
scope retains uploaded inputs for one day from first issuance; renewing credentials does not
reset retention. DELIVERING or COMPLETE tickets reuse accepted work. Canceled jobs cannot receive
new credentials. Upload revocation retries after restart and until issued credentials have expired,
guarding a late concurrent registration. Gateway retention deletion remains separately durable.

Public and private endpoint files use the same schema:

```json
{"host":"transfer.example.edu","port":22,"username":"fl-transfer",
 "host_key":"ssh-ed25519 BASE64_PUBLIC_HOST_KEY","data_port_first":5000,"data_port_last":5099}
```

Set the private file's host to the gateway's Headscale-reachable address. Replace the example key
with the actual canonical Ed25519 public host key. Treat both files as trusted deployment config.
The public gateway exposes verification, while its private control API remains scheduler-only.
See [the terminal workflow](remote-client.md#submit-and-resume) for submission and retry usage.

`POST /api/jobs/<UUID>/downloads` accepts an owner-scoped immutable request UUID, public SSH key,
and artifact kinds. Identical requests reuse the read identity and an export of the same retained
manifest. WAITING exposes no read credentials. The scheduler persists ARTIFACT_PREPARE; the Mac
returns its protected export identity's public key. A private upload grant and ARTIFACT_PUBLISH
then move files through BBCP. After the accepted ACK, private gateway sealing hashes every file
before the export becomes READY. The resulting ticket grants public reads to the user's key.
Transient publication errors retry with new command IDs, a ten-minute export deadline and ten
attempts; terminal hardware state is unaffected by export failures. Worker replacement and lost
HTTP responses resume durable work. Source and read scopes revoke after deletion or retention
expiry. The private export credential and public verification token are separate authorities.

`GET /api/jobs/<UUID>/logs` authenticates the owner or authorized operator/admin, checks current
retention/deletion, and returns bounded checksummed byte pages. It uses LOG_READ on the existing
agent WebSocket and shares identical pending requests. Up to sixteen requests per job can wait
for an offline Mac, with thirty-second deadlines. Maintenance removes read receipts after sixty
seconds. This does not affect effectful command replay or hardware state. Macs automatically
upgrade local SQLite to version 4; log reads use the existing scheduler outbox. Updated Mac snapshots are
required for live reads. See [live monitoring](live-logs.md) for resume, EOF and outage behavior.

Notification configuration is optional and contains no job/cluster YAML fields. Provider secrets
remain in its protected file; PostgreSQL stores fixed safe content and destination identities.
Administrators inspect `/api/admin/notifications` and explicitly redrive failed rows. See
[notification operations](notifications.md) for supported events, warning policy, routing,
activation/backfill, provider acknowledgement, and duplicate-delivery limits.
