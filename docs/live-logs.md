# Live log reads and state following

The Mac owns the append-only UART log. Job state comes from durable transitions and snapshots,
independently of log content. Deploy the updated scheduler before agents, because older scheduler
schemas reject the new snapshot field. Live monitoring requires an updated Mac agent; older agents that
do not advertise log watermarks return WAITING until upgraded and reconnected. User hosts need
only public HTTPS access to the scheduler, with normal Google authorization.

## Read contract

`GET /api/jobs/<UUID>/logs?stream=stdout&offset=0&limit=65536` accepts only `stdout` or `stderr`,
nonnegative signed-64-bit offsets, and a byte limit from 1 through 65536. It authenticates the
job owner or authorized operator/admin before creating a read command or returning cached data.
Offsets beyond an advertised file size fail with LOG_OFFSET. A job without an assignment has
no stream yet; a terminal unassigned job has empty logs.

Responses are version-1 LogPage objects with READY or WAITING state. A head identifies the
job, stream, local file identity, advertised byte size, terminal state and retention deadline.
A READY chunk names its range, includes base64 data and SHA-256, and marks EOF only when its
next offset equals the terminal head size. Empty active chunks mean that the reader has caught
up; they do not imply completion. WAITING carries no bytes and can mean that a source watermark
or agent read is not available yet.

The CLI verifies scope, offsets, SHA, file identity and EOF before yielding bytes. By default it
captures the first available size as its stopping position. With `--follow`, it keeps reading
until terminal EOF. It does not decode UTF-8 or add newlines. `--stream stderr` selects the job's
stderr file while still writing its bytes to the terminal process's stdout, allowing redirection.
Authentication prompts and errors use process stderr.

## Durability and bounds

UART capture fsyncs appended bytes before recording a JOB_LOG event containing the stream and
range. Events contain no UART data. Agent snapshots advertise current watermarks. The scheduler
checks their assignment against the authenticated cluster and rejects duplicate stream heads.

LOG_READ uses a fixed UUID-scoped path, a maximum 64-KiB range and a thirty-second deadline.
The Mac checks deletion, expiry, identity and size before and after the read. It rejects symlinks
and nonregular files. A writable descriptor permits POSIX fsync on macOS before read receipt
persistence; the operation does not alter log bytes. Concurrent command replays share a lock,
and later replays recheck retention and deadline before returning persisted bytes.

PostgreSQL uses its existing command outbox for read deduplication and receipt persistence,
so log reads need no additional scheduler table. The current scheduler requires migration
`0010_notification_delivery` for its notification consumer. Mac SQLite automatically
upgrades to version 4, adding optional command-receipt expiry. Existing effectful command receipts
remain permanent. Read locks live only while callers use them.

Each job allows at most sixteen pending read commands. Scheduler maintenance closes expired
requests after thirty seconds and removes read rows older than sixty seconds. The five-second
maintenance cadence can delay cleanup slightly. The Mac removes expired read receipts during
its thirty-second sweep. Read failures and these cleanup operations never change hardware outcomes.

Ownership, an artifact-deletion request, terminal retention and the latest advertised head are
checked before any cached bytes can be returned. Deletion blocks reads immediately even if the
Mac is offline and physical removal is delayed. These checks also apply to requests that reuse
an existing receipt. A user who has already saved output keeps that local copy.

## Following and outages

`submit --follow` and `status --follow` replay ordered state-transition events, then read current
job metadata. Terminal-looking log text or an event alone cannot end a monitor before metadata
reports a terminal state. Status without `--follow` returns the usual complete metadata object.

Log byte positions and event cursors advance only after delivery to the caller. Lost responses
are retried from the same position. Temporary transport and HTTP 408/429/502/503/504 responses
are retried; authorization failures stop the monitor. Refresh locks are held only for credential
rotation, independently of polling waits. Ctrl-C ends monitoring without changing the job.

While the Mac is disconnected, authorized unexpired cached ranges can be read. Other ranges
wait for a new read ACK, and running hardware continues locally. File replacement or truncation
fails rather than combining bytes from different sources. Retained final logs and other artifacts
can also be retrieved through the independent rclone/SFTP `results` workflow.

Linux tests cover real PostgreSQL and WebSocket execution, binary output larger than a page,
logs before completion, lost HTTP responses, scheduler reconnection without repeated execution,
shared concurrent reads, optional empty stderr, integrity failures, revoked ownership, retention
fences, bounded offline work, and expiring Mac read receipts while cancellation receipts survive.
Native macOS installation and hardware acceptance remain tracked in the requirement ledger.
