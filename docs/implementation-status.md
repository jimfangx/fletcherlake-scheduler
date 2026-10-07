# Requirement ledger

This ledger preserves implementation and regression evidence for `context/INST_Agent.md`.
The [requirement audit](requirement-audit.md) maps every numbered section to source and checks.
The software implementation is complete within the requested scope: supplied PCB firmware and
actual Kasa communication are excluded. Linux acceptance is recorded below. Native Mac,
live-provider deployment acceptance remains pending. BWRC licensing acceptance applies only
when the optional relay is enabled; the current Vivado Lab setup does not require it. Passing these tests
does not establish that an operational production platform has been deployed.

The subsequent Vivado Lab correction makes `license_relay` opt-in: omitted or null inventory
emits no relay configuration, HAProxy drop-in, license DNS record or license ACL/tag. Existing
complete mappings retain forwarding. All **29 focused deployment checks pass**, including
native parsers with and without the relay and the real enabled-relay TCP fixture. Ruff passes
for 350 Python files and strict mypy for 201 source modules. The historical 341-test run below
predates this deployment-only change.

Google Groups also supports `FL_GOOGLE_GROUPS_BACKEND=cloud_identity`: a service account
made an owner of the configured Workspace groups reads direct human membership without
domain-wide delegation or administrator impersonation. Existing delegated Directory setups
remain supported. All **54 focused Google authentication checks pass** (4.33 seconds),
including actual service-account assertion signing, intercepted group/membership HTTPS,
expired/removed memberships, permission failures, redirect/resource validation, protected
credentials, cancellation during token refresh, and real PostgreSQL session/role checks.
Ruff passes for 353 Python files and strict mypy for 202 source modules. The historical 341-test run
below predates this authentication change; live Workspace group-owner acceptance is pending.

The requested rclone migration supersedes the original BBCP transport requirement.
Current user upload, private Mac fetch, private Mac publication and public results download
use shared rclone/SFTP over pinned OpenSSH. The gateway serves a manifest-scoped SFTP v3
filesystem, preserves size/SHA verification, per-grant locks, source restrictions, revocation,
atomic publication and retention, and exposes no general shell/filesystem. All payload hops
are encrypted; the old independent payload-port range is removed from manifests, endpoint
serialization and ACLs. Install the checksum-pinned rclone v1.75.1 on users/Macs and follow
[the coordinated deployment migration](transfer-gateway.md#migrating-an-existing-bbcp-deployment).
Native Mac transfer and production throughput remain operational gates. The BBCP records
below describe historical checks, not current rclone performance.

Current rclone verification (2026-10-06): **417 full-suite tests passed, one optional
long-partition test skipped**, with two upstream deprecation warnings (567.11 seconds).
This run enables real PostgreSQL, checksum-pinned rclone/OpenSSH, Headscale 0.29.4,
Tailscale 1.102.4, Chromium and native deployment parsers. It covers all four artifact hops,
eight concurrent upload clients, empty and 64 MiB files, integrity/revocation/retention,
normal and forced DERP paths, source-bound key rejection, and terminal results retrieval.
Final SSH quoting/pin/identity checks add **9 passing focused tests**, including actual
transfers with Unicode, spaces, quotes and backslashes in identity paths. **8 focused retry
tests pass**, including temporary `RCLONE_MISSING` during Mac fetch and result publication.
**29 final deployment/private-network checks pass** with the gateway-to-Mac transfer rule
removed, native Linux parsers, and final SSH settings. They repeat input delivery and result
retrieval through normal and forced DERP peers.
Ruff checks/formatting pass for 354 Python files and strict mypy for 202 source modules.
All 77 README/documentation shell fences parse; the deployment guide's 49 fenced blocks,
stub AWS/GCP provisioning, protected credential generators and actual renderer pass local
validation. These checks do not provision live cloud resources.

Historical full-suite verification: **341 tests passed, one long-partition test skipped**, with
real Headscale 0.29.4, Tailscale 1.102.4 userspace peers, checksum-pinned official BBCP,
Chromium and native Linux deployment tools enabled (657.61 seconds). It includes the combined
private-network scenarios with normal and forced local DERP transport, native SSH source
rejection, private HTTPS/WSS, terminal execution and result retrieval, live cluster addition,
coordinator restart and cached private DNS, plus ACL isolation under both transport modes,
actual scheduler SIGKILL/restart and Mac reconfiguration management-process crash recovery.
CLI failure paths reject malformed YAML and misplaced secret fields without echoing their
values. The prior full suite
had **301 tests passed** with real wall-time partitions enabled (`FL_TEST_LONG_PARTITIONS=1`);
the recent gateway allocation and test-harness changes leave the agent/scheduler partition
protocol unchanged. Ruff checks/formatting pass
(349 Python files), and strict mypy passes (201 source modules).
The official network-tool downloader
and BBCP source/build helper completed successfully. The suite includes
disposable PostgreSQL migrations, enrollment/provider races, lost registration responses,
atomic drained-state unregister, session fencing, database cancellation, and existing
agent/WebSocket/crash recovery, real BBCP/SSH upload and pull, gateway control/staging authority,
revocation during IO/verification, retention deletion retry after restart, authenticated delivery,
Mac fetch verification, resumed delivery, bounded transient retries, cancellation during payload
IO over a live WebSocket, superseded-session ACK rejection, concurrent submission nonce replay,
reservation-bound public grants, upload cancellation/revocation retries, automatic input-free
execution, protected receipt rejection of changed keys/files, and terminal resume after lost
creation/delivery responses without duplicate jobs or execution while sending ELF and bitstream,
automatic approval for missing/expired sessions, provider outage behavior, and receipt owner
binding before submission, Mac terminal export/private sealing/public BBCP results and input
retrieval, download nonce/owner/deletion fences, independent client SHA and no-clobber files,
lost download response recovery, export-worker replacement and sealing replay, deletion during
blocked publication, explicit public read scope boundaries, and mid-stream gateway revocation.
Private publication cannot be sealed through the public staging endpoint. Live log checks cover
binary UART output before completion, paging beyond 64 KiB, shared concurrent reads, scheduler
reconnection and lost HTTP responses without duplicated bytes/execution, independent SHA and
scope validation, CLI stdout/stderr separation, ownership and deletion/expiry checks before
cached bytes, bounded offline reads, expiring Mac read receipts with durable cancellation
receipt preservation, and state-event replay that terminates only on authoritative metadata.
The legacy agent schema upgrade preserves queued jobs, events and effectful receipts. Notification
checks cover intercepted Mailgun/Slack/Google Chat envelopes and positive ACK validation,
redirect rejection, protected settings and secret rotation, concurrent projection, late commit
ordering, ingestion-time activation/backfill under Mac clock skew, bounded retry budgets,
route-wide pacing/Retry-After, lost provider responses with stable identities, lease fencing,
worker cancellation/replacement, destination removal, administrator-only inspection/redrive,
real agent success/warning/deletion fanout, source deadline warnings without UART interpretation,
serialized downtime events, and retention batches advancing beyond 100 jobs without duplicates.
Lifecycle contracts cover shared CLI/Python management, native macOS/privilege gates without
library sudo re-execution, unchanged caller inventory, stable UUIDs, closed HTTP clients,
confirmation ordering, persistent invalidation before drain/parsing/editing, rejected hardware
shutdown blocking reboot, and unregister acceptance before native teardown. Local collateral
deletion also reaps a blocked publication before physically removing its source files.

The subsequent reconfiguration crash audit found a recovery gap after native service removal:
confirmation attempted `bootout` again merely because the plist remained installed. Installation
now queries native registration and skips removal for an unloaded service. Real management
SIGKILL checks cover editing, dependency sync and completed service removal, using a real Unix
socket agent with mock hardware. They preserve UUIDs, queued jobs, terminal retention and input
hashes; incomplete configurations reject execution, and confirmation runs the queue once.
The service-removal case fails against the prior behavior. All 46 focused lifecycle, retirement,
enrollment and crash checks pass (19.59 seconds). The native-query unit test now mocks its
shared provisioning owner; 35 remaining unit checks pass (0.54 seconds). The final full suite
includes these regressions. Privilege, Pixi and launchd effects remain injected; actual Mac
launchctl is unverified.
See [macOS recovery operations](macos-operations.md).

Permanent retirement has a protected, replayable destroy journal, scoped public unregister
retry after node revocation, native launchd retention runtime checks, atomic UUID-bound SQLite
archives, independent expiry cleanup, and fresh re-enrollment. SIGKILL checks cover both sides
of archival and deletion commits; PostgreSQL checks cover undelivered reservation cancellation,
acknowledged physical-board reclamation, concurrent claims, stale HELLO/session fencing,
missing credentials blocking unacknowledged teardown, and
retained assignment ownership. The full lifecycle integration interrupts/cancels old work,
loses the unregister reply after node removal, enrolls a new UUID on the same boards, executes
fresh work, and expires old collateral without touching the new database.
Local Textual views display daemon snapshot tables, metrics and selected job details, preserve
selection and stale data, and serialize refresh reads. The configuration editor has explicit
three-slot board editing, host/tool/power fields, validated YAML firmware mappings, scoped
save/cancel behavior and schema errors. Headless interaction includes an 80×24 terminal and a
running real agent with mock hardware; CLI reconfiguration opens only after durable marker
invalidation and drain. Source models are not mutated and detection never invents boards.
The React/TypeScript scheduler dashboard implements all seven required routes, owner-filtered
jobs, reported board state and queue counts, cluster drain, cancellation/deletion controls,
administrator enrollment and known users. The optional static mount serves only explicit UI
paths and assets, with same-origin CSP and no API fallback. Six frontend tests and the locked
production build pass. Two real Chromium scenarios run inside the full Python suite against
PostgreSQL and two local HTTPS origins: cookie-bound OAuth redirect/callback, all routes and
reload, owner/admin denial, accepted cancellation without fabricated hardware completion,
masked in-memory enrollment issuance/revoke and navigation removal, logout/reload, desktop
and 390-pixel layouts, and scheduler/provider request-origin confinement. Injected Google and
Headscale providers make no external calls. Polls serialize, cancel on navigation and retain
marked stale data through transient failures while clearing unauthorized metadata. Human
session refresh is shared; network/5xx mutation failures are not replayed. Node build tooling
is isolated from the Mac agent's default Python environment. Linux CI enables browser acceptance;
the workflow itself has not run remotely. See [dashboard operations](scheduler-dashboard.md).
The larger scheduler workload concurrently submits 315 mixed-priority/cost/resource jobs behind
nine running blockers. Competing maintenance ticks preserve reservations and balanced placement;
queued cancellation, a restarted Mac connection and seven pending-again enqueue receipts precede
per-board priority/FIFO execution with no duplicated starts. The test rejects command-backlog
warnings and unhandled ASGI errors. It exposed and fixed unbounded command replay and snapshot
buffering: the gateway now delivers 32-command batches beneath the 64-command dispatch bound,
prioritizes cancellation after staging/enqueue dependencies, and the Mac permits one outstanding
snapshot with a matching ACK and a thirty-second deadline while receiving commands independently.
Focused PostgreSQL/protocol checks cover these bounds, dependency ordering, cursor correlation
and responsive commands. Closing transports are treated as disconnects; concurrent senders stop
before a protocol close.
Real 10/60/600-second partitions run concurrently across three executing agents behind TCP fault
proxies while the scheduler remains running. The final report measures **10.005 / 60.002 / 600.003
seconds**. Running hardware and queued work persist throughout, another job completes offline,
the event cursor remains unprojected until reconnection, default health becomes OFFLINE after
the long outage, and queued cancellation/success/events reconcile exactly once. Proxy shutdown
with live connections has a regression check. The earlier full suite with long partitions
enabled included both gates; the manual long-acceptance CI workflow has not run remotely.
See [scheduler acceptance](scheduler-acceptance.md).

The separate scheduler-process gate uses production HTTP/WSS and default maintenance against
real PostgreSQL with injected identity providers. SIGKILL kills the scheduler while mock hardware
continues; another board completes offline with no premature cursor projection. A new process
and database pool accept the original human session, fence the previous agent session, reconcile
offline success, cancel queued work before execution and replay four enqueue commands once.
A new public submission also verifies resumed automatic placement and enqueue. The focused test
passes (42.99 seconds), with a protected report of PIDs, cursors and replay identities. The final
full suite includes this gate. It does not establish systemd boot or external provider setup.
See [scheduler acceptance](scheduler-acceptance.md).

The non-secret Linux deployment manifest now
renders separate scheduler/gateway/license bundles, explicit public/private listeners, protected
service defaults, pinned SSH endpoints, private DNS and matching transfer/license ACL ports.
It rejects injection, secret fields, duplicate roles, wildcard bindings and unsafe path forms;
spaces, quotes and percent signs are handled by each native configuration format. Rendering
refuses existing destinations and leaves installed services alone. Twenty-four focused checks
pass, plus two sanitized CLI failure checks, including actual nginx, HAProxy, sshd, Headscale
config/policy and systemd parsers. Systemd
standard dependencies and distro proxy base units are isolated parser stubs, not service boots.
Real local TLS nginx enforces public/private route boundaries, forwarded-header replacement,
WebSocket upgrades, 429 rate limits, 413 payload limits, the larger unregister body allowance
and OAuth query-log exclusion. A real HAProxy process forwards both fixed private ports while
another interface/port is denied. Explicit loopback/high-port mappings keep these fixtures
independent of production interfaces without weakening production validation. Linux CI enables
native checks; nginx is isolated from Python/macOS and Node environments, and HAProxy source
is checksum-pinned. These checks do not prove systemd boot, production certificate issuance,
operational DERP fallback, Mac DNS/TLS or actual BWRC checkout/release for an enabled relay.
See [Linux deployment](linux-deployment.md).
Combined private-network acceptance now enrolls real coordinator/WireGuard peers around
native BBCP and the production terminal/agent/scheduler/gateway workflows. Public terminal
clients remain outside the private network. Private HTTPS control, outbound WSS, scoped fetch,
source-bound native SSH authentication, mock execution, private output publication/sealing and
public result retrieval are exercised together, including independent hashes and submission
replay without another execution. Both scenarios run with normal paths and forced local DERP
transport; relay-only variants disable IPv4/IPv6 UDP and check private peer counters and the
absence of direct addresses. The real embedded relay verifies enrolled clients; its test-only
HTTP listener is loopback. Linux socket adapters preserve the actual assigned `/32` in native
OpenSSH checks instead of widening grants. They do not establish native Mac TUN/DNS/BBCP,
production HTTPS DERP or real NAT/firewall traversal.
The real Headscale/embedded-DERP restart gate additionally forces relay-only transport and
keeps scheduler, gateway and peer processes alive through a measured **40.17-second** outage.
All three node identities survive with the original database and keys. Running mock hardware stays
active, another job completes offline without premature projection, and queued cancellation
arrives before execution after a new scheduler session. Success events project once. Internal
peer DNS answers are verified before, during and after the outage using cached network maps;
host resolver installation remains a native Mac gate. A protected JSON report records the
duration and job identities. This fixture changes no production protocol.
See [private-network acceptance](private-network-acceptance.md).
Repeated private SSH checks reproduced a stale TCP packet filter on Headscale 0.28.0: the
gateway knew the second cluster and exchanged encrypted packets, but its ingress rules
still listed only the first cluster. The downloader, configuration and adapter now pin
0.29.4. Focused API/transfer compatibility and the native configuration/policy parser pass.
Eight consecutive relay-only transfer/source-fence repetitions pass; every retained gateway
policy includes the second cluster's real address for TCP/22. The final full suite also passes
with the revised pin. Protected snapshots retain only public transport state and packet policy,
with no full network map, private keys or user profiles.
A live-enrollment test adds two clusters to an already serving gateway, and peer/port
isolation now runs with both normal and forced relay paths. Linux CI retains the filtered
reports for seven days; that workflow has not run remotely.
A separate repeated check timed out during public BBCP upload after private SSH readiness
and correct gateway policy. Its cause remains unexplained; the final eight instrumented
repetitions and the final full suite passed.
Separate concurrent public-only acceptance reproduced native BBCP receivers failing with
`Address already in use`. Forced SSH commands now reserve distinct available ports using
OS-held locks within the configured range; waiting consumes the existing credential deadline
and cleanup reaps the native process before releasing its reservation. Eight independent
clients each upload two unique artifacts under both an eight-port and the default range.
All payloads and immutable manifests verify separately. Real-socket/process tests also check
occupied-port deadlines and lease recovery after SIGKILL. Nineteen focused transfer checks
pass (12.40 seconds). This fixes the reproduced bind failure; it does not establish the cause
of the earlier public-upload timeout. The final full suite includes these checks and passes.
Provider tests send no external messages. Two upstream
Starlette TestClient deprecation warnings remain.
These checks do not prove deployment on a Mac or actual license checkout.

| Milestone | Current implementation and evidence | Remaining acceptance work |
| --- | --- | --- |
| 0 Repository/CI | Pixi monorepo, lock for linux-64/osx-arm64, Python package layout, Ruff, strict mypy, pytest, Linux/macOS workflow; separate locked Node web environment, frontend checks/build and Linux browser acceptance; manual real-duration partition/load CI workflow | CI itself has not run remotely |
| 1 Shared models | Cluster/Board/Environment/PowerControl, JobConfig/JobSpec, artifacts, shmoo, states/events, bounded log pages/watermarks, command envelopes, snapshots, roles, reservations; serialization/bounds/identity tests | None for the software implementation |
| 2 Hardware/simulator | BoardBackend, MockBoardBackend, execution results, sensor/voltage/frequency/UART; subprocess adapter tests; nine-board execution and 315-job workload plus real-duration outage simulation | Physical backend acceptance remains in milestone 11 |
| 3 Durable agent | SQLite WAL transactions, priority/FIFO queues, exclusive workers, events, TTL/deletion repair, cache, recovery; SIGKILL tests across QUEUED/programming/RUNNING/CANCELING and deletion | Native Mac reboot validation |
| 4 Local CLI/SDK | fl cluster status; fl job submit/kill/status; direct Python API and context-managed client; shared ClusterSetup init/confirm/reconfigure/restart/destroy; private Unix socket; API/lifecycle/enrollment integration tests | Native Mac terminal validation |
| 5 Provisioning | Shared CLI/Python workflows; macOS autodetection, overrides, interactive host/board/tool editor, init/confirm/reconfigure/restart/destroy, protected retryable enrollment, key-file join, stable UUID reconfiguration, durable marker invalidation, readiness retry, launchd generation; replayable destroy journal, public unregister retry, atomic retired history, independent launchd retention, fresh UUID/database re-enrollment and physical-board reclamation; injected native commands/public HTTP/PostgreSQL/SIGKILL/headless Textual tests | Actual macOS terminal, launchctl, runtime, sleep/wake and hardware validation |
| 6 Headscale | Pinned 0.29.4 adapter, config/strict ACLs, admin-only tickets, short-lived nonreusable keys, encrypted retry receipts, verified node registration, durable revocation cleanup; actual coordinator + three userspace Tailscale peers prove permitted traffic and denied cluster/port access; consistent deployment renderer, native config/policy/systemd/nginx/HAProxy checks, real local private/public HTTPS and fixed-port relay acceptance; combined native rclone/SFTP/private HTTPS/WSS workflows and forced local DERP transport | Production service boot, actual Mac DNS/TLS/private-agent connection, production HTTPS DERP and real NAT/firewall traversal; BWRC license checkout/release only when the optional relay is enabled |
| 7 Scheduler | PostgreSQL registry, frozen Alembic migrations through 0010, capped queue scoring, niceness/FIFO, concurrent-safe reservations, heartbeat health, durable command outbox, bounded WebSocket command batches and snapshot ACK flow control, session fencing, authenticated public APIs, enrollment, atomic final-state unregister, idempotent public submission/owner-bound reserved upload grants, automatic input-free enqueue, durable terminal artifact export and public read scopes, owner-checked bounded live log reads with expiring outbox receipts, service entrypoint, maintenance/cleanup and durable delivery/upload revocation loops; real PostgreSQL/nine-board/315-job and 10/60/600-second partition tests | Deployment/runtime checks and actual Mac connection |
| 8 Scheduler execution | Real WebSocket→three agents/nine mock boards→replicated outcomes; success/failure/timeout/cancel, receipt replay, scheduler restart; actual rclone/SFTP upload→gateway→Mac fetch→independent SHA→durable ACK→enqueue→mock execution; delivery-worker restart, transient retry, cancellation during blocked payload tests; 315-job mixed workload with concurrent placement, cancellations, safe replay and priority/FIFO execution; real 10/60/600-second TCP partitions preserve hardware/queues, offline completion and exact event reconciliation; combined private HTTPS/WSS/rclone/SFTP workflows over actual Headscale peers, including forced local DERP | Actual Mac acceptance; production service boot and networking |
| 9 Authentication | Google code/PKCE/nonce login with maintained ID-token verifier, group-owned Cloud Identity or delegated Directory lookup, direct membership/expiry checks without admin impersonation for Cloud Identity, <=60s membership cache, one-use terminal approval, hashed/rotating/revocable sessions, owner/role enforcement, fl-client login integration; PostgreSQL/HTTP negative tests; real nginx auth/bootstrap rate limits and query-log exclusion | Live Google Workspace configuration/acceptance and public proxy deployment |
| 10 Transfer/remote client | Local storage hashes/verifies copied inputs; fl-client login/logout/jobs/status/cancel/logs, submit/status follow, submit/resume and results with protected owner-bound credentials/receipts, automatic terminal approval for absent/invalid sessions, and serialized automatic refresh; idempotent owner-scoped request IDs, reservation-bound upload grants, token hashes only on the scheduler, durable upload revocation; terminal→public API→real rclone/SFTP ELF+bitstream→gateway verification→Mac fetch→mock execution, lost creation/delivery responses resume one job without duplicate uploads/execution; retained results/logs/job/inputs return through Mac publication→private gateway sealing→explicit public read scopes→independent client SHA→atomic no-clobber files, replay reuses verified local files; exact retained export manifests, worker replacement, bounded retries, publication cancellation on deletion, source/read revocation and per-chunk sender checks; shared rclone/SFTP transport, pinned SSH host keys, protected rclone/known-hosts configuration, private control/public authority separation, manifest-only SFTP filesystem, immutable verified uploads, transactional revocation/retention; Mac rclone tool configuration; checksummed binary log paging/follow with offset retry, durable event replay and authoritative metadata termination, deletion/expiry fences, bounded offline reads and short-lived receipts; native source-bound SSH and terminal submission/results through actual Headscale peers with normal and forced local DERP paths | Validate native Mac rclone/OpenSSH transfer; native DNS/TUN and production service boot |
| 11 Firmware | Configurable LilikoiBoardBackend wraps all named operations without scheduler-specific syntax | Validate actual supplied firmware mappings on Macs when firmware is available |
| 12 Dashboards | Local Textual Boards/Jobs/Queues/Artifacts/System tables, metrics, job details, serialized read-only refresh, stale-data recovery, literal text rendering and keyboard navigation; headless interaction and real local daemon acceptance. React scheduler cluster/board/job routes and details, owner filtering, enrollment/users administration, accepted command controls, secure cookie sessions, bounded refresh, snapshot counts and explicit static serving; focused frontend and production Chromium/PostgreSQL/HTTPS acceptance on desktop/mobile | Actual public deployment/Workspace login and native Mac terminal acceptance |
| 13 Notifications | Mailgun, Slack and Google Chat adapters; atomic destination-scoped event projection, durable activation/backfill using ingestion time, late-commit/concurrent deduplication, leased and fenced delivery, bounded persisted retries, shared route pacing/Retry-After, protected configuration, safe content/error projection, administrator inspection/redrive with preserved intent and attempt history; native deadline-budget warnings, serialized downtime/recovery events, bounded once-per-expiry retention warnings and durable deletion; intercepted-provider/real PostgreSQL/agent WebSocket acceptance | Configure deployment destinations and credentials; live provider acceptance and native Mac/hardware warning policy validation |
| 14 Kasa scaffold | Versioned optional config, local editor and scheduler UI representation, power-state enums, unsupported force-off protocol, PostgreSQL optional fields, future role permission; no power-on operation | Actual plug communication is explicitly out of scope |

Architecture rules have source and integration evidence in the requirement audit, including
the terminal workflow, private-network isolation, owner/role checks, independent SHA verification,
browser request confinement, durable recovery and scheduler/network outages. Production listener
and firewall configuration still needs validation on the deployment hosts.
