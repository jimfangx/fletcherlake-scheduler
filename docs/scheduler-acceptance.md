# Scheduler load and connection-loss acceptance

These tests exercise scheduler decisions through real PostgreSQL and TCP/WebSockets into
three simulated Mac agents with three durable SQLite board queues each. Firmware stays behind
`MockBoardBackend`. They require local socket/process access but no physical PCB, Google
credentials, Linux TUN device or change to the Mac architecture.

## Larger workload

```sh
pixi run pytest -q tests/integration/test_scheduler_load.py
```

The test holds one running job on every board, concurrently submits 315 additional jobs with
mixed niceness/runtime estimates and explicit/resource-based placement, and runs competing
production maintenance ticks. It verifies balanced assignments across nine boards, every
explicit constraint, acknowledged durable queues, and cancellation before queued execution.
A Mac connection is restarted with seven acknowledged enqueue receipts made pending again;
the existing command identities must replay safely. After canceling the nine blocking jobs,
each surviving job must execute exactly once in its board's priority/FIFO order. Canceled
queued jobs must never start. This is a correctness workload, not a throughput benchmark:
the firmware and maintenance polling interval are simulated.

The production gateway sends at most 32 pending commands per replay batch, leaving capacity
under the agent's 64-command dispatch bound. PostgreSQL prioritizes cancellation past long
fetch/publication commands after the job's staging/enqueue acknowledgements are durable.
Unacknowledged local-job creation remains ahead of cancellation, so a premature JOB_NOT_FOUND
cannot become the cancellation's persistent receipt. Unbounded inspection of the outbox retains
its original creation ordering. Batch selection and this dependency fence have a focused real
PostgreSQL test. Agent connection logs expose safe protocol error codes without credentials.

An agent sends one outstanding status snapshot at a time and waits up to thirty seconds for
its matching ACK before reconnecting. Unrelated acknowledgements cannot advance its event
cursor. Command reception and replies remain independent during that wait. This bounds
snapshot buffers when PostgreSQL projection is slower than the heartbeat interval. The
gateway also treats a transport closing during a projection/reply as a disconnect and stops
concurrent senders before a protocol close. The workload rejects command-backlog warnings and
unhandled ASGI errors; a focused protocol test covers delayed ACKs and responsive commands.

## Scheduler process death

```sh
pixi run test -q tests/crash/test_scheduler_restart.py
```

This scenario starts the production HTTP/WSS composition and its default maintenance loop
in a separate process with its own PostgreSQL connection pool. Public submission and subsequent
authentication use verified local HTTPS; the agent connects over WSS with the fixture CA trusted
through `SSL_CERT_FILE`. Google identity and Directory providers are injected, and hardware is
mocked. Enrollment, rclone/SFTP and notification delivery are exercised by their separate scenarios.

After two jobs start and two wait in a durable queue, the scheduler receives SIGKILL. One
board stays running and another completes offline without advancing the PostgreSQL event cursor.
The test makes four persisted enqueue acknowledgements pending again and records a queued
cancellation through production control code using an independent database connection while
the scheduler is down. These are deliberate fault inputs, not public requests to an offline service.

A new process opens a fresh pool on the existing database and listener. The existing human
session authenticates again, the agent establishes a new session, offline completion projects,
and cancellation arrives before execution. A new public submission proves maintenance resumes
placement and input-free enqueue. Each surviving job starts once, success events project once,
and the prior agent session is fenced. A protected `scheduler-restart-report.json` records PIDs,
cursors and replay identities without credentials. This checks process recovery; production
service-manager boot and external provider configuration remain separate gates.

## Required outage durations

```sh
FL_TEST_LONG_PARTITIONS=1 FL_PARTITION_REPORT=/tmp/fl-partitions.json \
  pixi run pytest -q tests/integration/test_network_partitions.py
```

This opt-in check takes approximately eleven minutes. The three agents undergo concurrent
10-second, 60-second and 600-second partitions using separate local TCP fault proxies. A
partition closes the active TCP/WebSocket and rejects reconnect attempts while the scheduler
listener remains running. No simulated clock or shorter interval substitutes for elapsed
wall time. The reconnect loop, heartbeat health and replicated event cursors are production
code; mock hardware run durations exceed each partition.

During every outage, one job must remain running locally and two jobs must remain durably
queued. Another board completes a job offline; the scheduler must retain its last reported
RUNNING state until reconnection, rather than invent completion. A cancellation requested
through the scheduler during the outage cannot affect the isolated local queue until command
delivery resumes. Default heartbeat health sweeps move the ten-minute peer to OFFLINE without
interrupting its running hardware.

After traffic is restored, the real exponential reconnect delay applies. Snapshots and event
replay must reconcile offline completion, deliver the pending cancellation before it runs,
execute the surviving queue once, and project exactly one success event for each executed job.
The optional protected JSON report contains measured durations and job counts, with no tokens
or credentials. Regular pytest explicitly skips this long test unless enabled. The manual
`long acceptance` GitHub Actions workflow enables both tests and retains the duration report;
the workflow must be run on GitHub separately to establish hosted CI evidence.

These gates complement [rclone/SFTP/gateway verification](transfer-gateway.md) and
[Headscale peer isolation](headscale-operations.md). [Combined private-network acceptance](private-network-acceptance.md)
now exercises real private HTTPS/WSS, rclone/SFTP fetch/publication and native SSH source fencing,
with both normal paths and forced local DERP transport.
Its coordinator-restart gate stops the real Headscale/DERP process while the scheduler and
agents remain alive, then verifies preserved identities, offline completion, pending cancellation
and event replay over a new session. Internal peer DNS answers are checked across that outage.
[Deployment rendering](linux-deployment.md) is also checked with native parsers and proxies.
Actual macOS/hardware/license/provider and production HTTPS DERP checks remain separate work.
