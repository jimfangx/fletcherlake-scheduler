# Architecture and durability boundaries

## Modules

`fl_common.models` contains strict version-1 Pydantic schemas. User `JobConfig` rejects trusted
fields such as owner, UUID, submission time, and board assignment. `JobSpec.from_config` adds
trusted metadata and expected content references. Transfer verification gates execution.
Unknown schema versions
are rejected so a new wire version must have an explicit migration/negotiation path.

`fl_agent.service.AgentService` owns lifecycle and submissions. Production `fl-agent` listens
on `/var/run/fl/agent.sock`, protected by the service's umask and runtime-directory permissions.
This is an administrative local interface: access to its socket permits selecting local input
paths readable by the daemon. It is not a public or multi-user authentication boundary. The
scheduler connection is a separate authenticated outbound transport.

`BoardWorker` owns one backend and one OS board lock. An agent-wide state lock and runtime lock
also prevent overlapping daemons. Each board claims work from SQLite, in priority/FIFO order.
A queue claim checks that the board is idle and healthy. Canceling queued work does not overwrite
another job's board ownership. A failed stop quarantines the board until successful recovery.

`Executor` performs input verification, FPGA cache checks, SoC programming, and UART collection.
`ShmooStrategy` isolates sweep policy from execution. Voltage and frequency are searched as
independent monotonic discrete axes, recording all samples. Shmoo jobs and failed executions
power down the board; the cache is invalidated when physical state becomes uncertain.

`LilikoiBoardBackend` binds operations to trusted argv templates, without assuming firmware CLI
syntax. Missing commands fail explicitly. Completion is an explicit backend operation returning
`RunResult`; no executor attempts to infer success from UART text. Firmware commands use process
groups, capture both output streams, and are terminated and reaped on cancellation/deadline.

## Persistence

SQLite uses WAL, FULL synchronous mode, foreign keys, and short BEGIN IMMEDIATE transactions.
A transition writes the job state, queue membership, active-board metadata, and event together.
Terminal transitions activate retention timestamps. File IO and hardware work occur outside
these transactions. `schema.sql` creates version 1; the ordered upgrade in `AgentDB` adds the
version-2 retention completion marker, version-3 per-job event index, and version-4 optional
expiry for short-lived read receipts. Effectful command receipts keep their replay identity.
Future migrations must
preserve queued jobs and events.

Local input staging creates a durable CREATED/STAGING record before copying. Copies stream
through a temporary file, hash the actual copied bytes, fsync, and atomically replace the fixed
UUID-scoped target. A cancellation reaps any outstanding copy thread before storage closes.
All internally chosen filenames are fixed (`binary`, `bitstream`, `stdout.log`, etc.). Each
shmoo sample is an immediate durable event; interrupted sweep results recover those samples
without relying on an in-memory result list.

Terminal collateral indexing and expiry installation have a durable completion marker. Boot
and periodic repair complete interrupted indexing. Deletion first creates a PENDING record,
removes files idempotently, then marks records deleted and appends an event in one transaction.
A crash after file removal therefore retries safely and produces one ARTIFACT_DELETED event.
Retention begins at completion rather than submission, keeping inputs through long runs.

After process death, queued jobs remain queued. Active jobs become INTERRUPTED; a previously
acknowledged cancellation becomes CANCELED. Hardware is stopped and powered down before the
board begins new work. Previously active execution is never automatically resumed.

The FPGA fingerprint cache is cleared on every daemon initialization, making it conservatively
ephemeral even if a caller supplies a persistent runtime path. Only successful programming
writes a fingerprint. Restart and board-reset paths invalidate it.

## Configuration lifecycle

`cluster.yaml` must validate, and `cluster.sha256` must match its exact bytes before jobs can
start. A missing or mismatched marker means CONFIGURATION_INCOMPLETE. Reconfiguration removes
the marker before editing. A removed board with pending work blocks execution rather than
silently discarding that queue. DRAINING persists across a daemon restart; a planned Mac
RESTARTING returns to READY after recovery and configuration validation.

`fl.ClusterSetup` owns provisioning and lifecycle workflows shared by the CLI and Python.
The CLI handles prompts/sudo; library callers must already hold administrator privilege on
macOS. Both use the same daemon API for hardware drain. Provisioning generates macOS launchd
commands and performs retryable scheduler enrollment and
Headscale join with protected temporary receipts. Destroy unregisters a final authoritative
snapshot before removing credentials; scheduler revocation cleanup is durable. Protected
retirement journals and atomic cluster-scoped archives preserve history, while a hardware-free
launchd job continues TTL cleanup. Re-enrollment uses a new SQLite database/UUID, with physical
board ownership transferred only after acknowledged retirement and no active assignments.
Old job assignments keep their original cluster. See [retirement](retirement.md).
Actual Mac installation/network/firmware acceptance remains. Lifecycle events replay through
the agent connection.

## Scheduler protocol and reservations

PostgreSQL keeps inventory, assignments, indexed agent records, and an outbox of commands.
Placement serializes reservations across scheduler instances and enforces one active transfer
reservation per board with a partial unique index. Niceness and FIFO choose pending jobs;
capped queue cost chooses compatible healthy boards. Once a command may have been delivered,
the assignment no longer expires automatically: guessing whether the agent accepted it could
execute a job on two physical boards.

Agents make one outbound authenticated WebSocket connection. Commands retain their UUID until
acknowledged, and the agent persists receipts after durable effects. Snapshots update physical
state; event sequences have a separate contiguous replay cursor, preventing snapshots from
skipping notification events. A newer connection fences older snapshots and command ACKs. Network health
changes do not terminate or reassign hardware execution.

Production agent URLs require HTTPS/WSS. The daemon reads mode-0600 `credentials.json` from its
state directory, separate from cluster inventory. Explicit HTTP is available only to directly
constructed simulation connections. The Linux service also composes authenticated public REST,
Google authorization, enrollment cleanup, and durable transfer delivery.

## Collateral delivery

Public submission persists an owner-scoped request ID, metadata digest, job UUID, and public
upload identity in one transaction. A protected local receipt saves the private identity and
staging token before the first request. Repeating identical metadata reuses the job, including
after a lost response. The receipt records the authenticated owner before creation; switching
logins cannot resume that receipt under another user. Only an active board reservation permits
upload credential issuance; the scheduler registers the persisted scope through private gateway
control and rechecks
cancellation afterward. A durable cleanup loop revokes closed scopes and guards late registration.
No input collateral bytes pass through scheduler HTTP APIs. Jobs with no inputs bypass transfer and are
automatically enqueued by maintenance after reservation.

The scheduler asks the private gateway for a verified upload attestation, checks the complete
manifest against the owned JobSpec, and freezes the assignment before sending JOB_STAGE.
PostgreSQL records the delivery scope and deadline. The Mac creates a protected per-transfer
SSH identity and returns only its public key. The gateway grants read access to the immutable
upload, restricted to that key and the Mac's Headscale addresses. JOB_FETCH tells the Mac to
initiate rclone/SFTP; the scheduler executes no SSH command. A bounded concurrent command dispatcher
keeps heartbeats and cancellation responsive during payload IO.

The Mac downloads to a temporary file, verifies SHA and size, fsyncs, and atomically publishes
the fixed target. A durable fetch ACK gates JOB_ENQUEUE, whose existing verification checks
the inputs again. Process/connection interruption replays commands; already verified files can
be reused after checking their content. Mac process restart interrupts STAGING jobs under the
existing recovery policy. Private identities stay with job collateral and its retention lifecycle.

Transient fetch failures retry with new message IDs and the same scope. A delivery scan cursor
prevents old work batches from starving newer requests. Cancellation, permanent failure, and
expired delivery budgets block enqueue and revoke the private grant. Gateway revocation retries
after scheduler restart.

## Retained results

Public download requests authenticate the job owner or operator/admin, freeze an exact retained
artifact manifest, and save an owner-bound read identity in PostgreSQL. Identical concurrent
requests reuse it. The scheduler issues ARTIFACT_PREPARE to the assigned Mac; the Mac verifies
local files and durably binds an export identity before ACK. ARTIFACT_PUBLISH initiates private
rclone/SFTP upload under a source-IP-restricted scope. An accepted upload ACK is followed by gateway
SHA verification and sealing. READY exports grant public reads to a distinct user-generated key.
Neither private key nor a gateway control secret enters job metadata or agent snapshots.

Publication retries use fresh command IDs and the same immutable scope, bounded by a ten-minute
deadline and ten attempts. Export failure never changes completed hardware outcomes. Sources
carry their original completion-based retention deadlines. Deletion requests fence new read
credentials, cancel/reap active Mac uploads before removal, and trigger durable gateway revocation.
Gateway senders recheck permission and source retention between chunks. Late registration cleanup
continues until its issued credentials expire.

The user saves a protected download receipt before requesting a scope. rclone/SFTP reads from fixed
gateway paths to a temporary local file. Independent SHA/size checks and fsync precede publication;
an atomic no-clobber link protects unrelated files. Repeat requests reuse verified local content.

## Live monitoring

UART bytes are appended and fsynced locally. JOB_LOG records only the stream and byte range;
snapshots advertise file identity, size, terminal state and retention. The scheduler validates
that each watermark belongs to the authenticated cluster's assignment. Logs never determine
job state or execution success.

Owner-authorized HTTP reads issue bounded LOG_READ commands over the existing outbound Mac
WebSocket. File opens reject symlinks and unexpected identities; reads recheck deletion and
expiry before ACK. The response contains base64 bytes, offsets and SHA. Identical concurrent
reads share an outbox receipt, with at most sixteen pending reads per job. Thirty-second
deadlines and receipt sweeping bound offline work and stored bytes. Ownership and current
retention are checked before every cached response.

The client advances offsets after yielding verified bytes and resumes across temporary transport
failures. State following replays durable transitions and reads authoritative job metadata before
deciding to stop. Full retained artifact retrieval continues through rclone/SFTP; short live log chunks
use authenticated WSS/HTTPS. See [live monitoring](live-logs.md) and the requirement ledger.

## Notification consumers

Scheduler events have an independent ingestion timestamp for durable subscriber activation.
Route/event projection commits notification intent atomically and queries missing projection
identities, preserving lower IDs that commit late. The outbox freezes a small content projection
and recipients; provider keys and URLs stay in protected service configuration.

Delivery uses expiring leases, stale-result fencing, bounded durable retries and shared pacing.
Provider adapters validate acknowledgement and reject redirects. Failure, timeout or restart
never changes hardware outcomes. Administrator redrive preserves identity and attempt history.
External provider acceptance and PostgreSQL cannot commit atomically; see
[delivery guarantees](notifications.md#projection-retries-and-restart).

The Mac records a possible-hang event as its execution deadline approaches, without interpreting
UART silence. Scheduler health transitions and bounded retention scans produce downtime/recovery
and expiry warning events. Provider logic lives entirely outside these producers.
