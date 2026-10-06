# Implementation audit

The requested software implementation of `context/INST_Agent.md` is complete. This is an
implementation and Linux acceptance result, not a production deployment claim. The plan
explicitly excludes PCB firmware and requires only a Kasa scaffold; the user also allows
macOS-specific behavior to be implemented without being runnable on this Linux host.
Actual launchd, native Mac networking and physical firmware validation remain required before
operating real boards. Per the subsequent user correction, BWRC forwarding is optional for
the Vivado Lab Mac setup; omit `license_relay` or set it to `null`. Checkout validation applies
only to deployments that opt into a relay. Live identity/notification acceptance needs external
endpoints, credentials and entitlement that are unavailable here.

## Verification

- Optional-relay change: **29 focused deployment tests pass**, including native parsers for
  enabled/disabled bundles and the real opt-in TCP relay. Ruff passes for 350 Python files
  and strict mypy for 201 source modules. The full-suite result below predates this change.
- Full Python acceptance: **341 passed, 1 optional long-partition test skipped**, in 657.61s.
  This run enabled real PostgreSQL, pinned BBCP/OpenSSH, Headscale 0.29.4, Tailscale 1.102.4,
  Chromium, nginx, HAProxy and native Linux configuration validators. Two upstream Starlette
  deprecation warnings remain.
- Ruff lint and formatting: **349 Python files** checked. Strict mypy: **201 source modules**.
- React checks, **six frontend tests**, and the locked production build pass. Two real-browser
  scenarios are also included in the full Python acceptance run.
- The separate real-duration partition run measured **10.005, 60.002 and 600.003 seconds**.
  It was included in an earlier 301-test full run; the current full run skips this optional
  ten-minute gate. Subsequent gateway port and lifecycle changes did not change that protocol.
- The current full run includes an actual scheduler SIGKILL/fresh-process restart, agent
  SIGKILL recovery, reconfiguration and retirement crashes, a 315-job/nine-board workload,
  and normal/forced-local-DERP private transport. Restart tests inject identity providers and
  native Mac effects; they do not prove production service startup.

Repeat the standard commands with `pixi run test`, `pixi run lint` and `pixi run typecheck`.
Optional native tool variables, long acceptance commands and their exact scope are documented
in [scheduler acceptance](scheduler-acceptance.md),
[private-network acceptance](private-network-acceptance.md) and
[Linux deployment](linux-deployment.md). The checked-in CI workflows run these tools on Linux
and Python checks on macOS; the workflows have not been executed remotely.

## Requirement traceability

Paths below are relative to the repository root. Each row points to implementation and tests
that exercise behavior; a source reference alone does not prove native platform behavior.
The [requirement ledger](implementation-status.md) records all fifteen milestones and detailed
regression results. This table additionally covers every numbered plan section, including
requirements outside the milestone headings.

| Plan section | Implementation | Behavioral evidence |
| --- | --- | --- |
| 0–1 Goal and authority split | `fl_agent.service`, `fl_scheduler.service`, `fl_client.submission`; separate control, execution and transfer services | `test_websocket_execution.py`, `test_private_terminal.py`: scheduler → agent → mock hardware → replicated result and terminal retrieval |
| 2 Repository | `pyproject.toml`, `pixi.toml`, `pixi.lock`, `.github/workflows`; separate Python packages and Node environment | Standard test/lint/typecheck commands and locked dashboard checks/build |
| 3 Schemas | `fl_common.models`: versioned, bounded Cluster/Board/Environment/PowerControl, JobConfig/JobSpec, artifacts and events | `unit/test_models.py`: round trips, trusted-field rejection, relative inputs, board limits, duplicate identifiers |
| 4 Optional Kasa | `fl_common.power`, optional cluster/board config and database fields | `test_models.py`, `test_migrations.py`, setup/dashboard tests; unsupported interface has no physical plug action |
| 5 JobConfig/JobSpec | `fl_common.models.job`, agent submissions and scheduler creation | `test_models.py`, `test_public_submission.py`, `test_submission_receipts.py`: canonical configuration, server-owned identity, overrides and replay conflicts |
| 6 Hardware abstraction | `fl_agent.hardware.base`, `mock`, `lilikoi`; argv-only cancellable process runner | `test_hardware.py`, `test_agent.py`: simulated programming/run/UART/sensors and supplied command mapping, literal arguments, failure/timeout/cancel cleanup |
| 7 Mac daemon | `fl_agent.daemon`, `service`, Unix-socket `api`; launchd provisioning | `test_agent.py`, `test_lifecycle_sdk.py`: daemon owns execution; CLI/SDK call its API |
| 8 Board workers/queues | `fl_agent.board_worker`, transactional SQLite queue claims | `test_queue.py`, `test_scheduler_load.py`: exclusive workers, niceness/FIFO, concurrency and nine-board workload |
| 9 Durable local state | `fl_agent.db`, `schema.sql`, `recovery`; WAL/FULL transactions and exclusive locks | `crash/test_process_recovery.py`, `test_queue.py`: SIGKILL preserves queued jobs, events and expiry; active work becomes interrupted |
| 10 Artifact retention | `fl_agent.collateral`, `retired`, `retention`; durable expiry and deletion repair | `test_agent.py`, `crash/test_deletion_recovery.py`, `test_retired_retention.py`: restart-safe TTL and idempotent physical deletion |
| 11 Job state machine | `fl_agent.state`, `executor`, `board_worker` | `test_queue.py`, `test_websocket_execution.py`: illegal transitions roll back; success/failure/timeout/cancel/interruption replicate |
| 12 Bitstream cache | `fl_agent.cache` in runtime directory; successful-programming recording and invalidation | `test_agent.py`: identical SHA skips, forced reflash programs, graceful restart clears cache; native reboot still needs validation |
| 13 Setup/reconfiguration | Shared `fl.macos.management`, provisioning/detection/editor/enrollment; CLI delegates | `test_provisioning.py`, `test_lifecycle_sdk.py`, `test_inventory_editor.py`, `test_macos_enrollment.py`, `crash/test_reconfiguration_recovery.py`: overrides, durable SHA invalidation, drain, stable UUID and resumable confirmation |
| 14 Graceful restart | Shared management drain → shutdown ACK → native reboot | `test_lifecycle_sdk.py`, `test_agent.py`: queued jobs/TTL survive; failed hardware shutdown prevents reboot; native command is injected |
| 15 Future force-off | `fl_common.power`; future permission and optional stored power fields | Scaffold tests and migrations; actual Kasa communication is explicitly excluded |
| 16 Local submission | `fl_cli.main`, `fl.sdk`, `fl_agent.submissions` | `test_agent.py`, `test_models.py`: YAML/overrides → direct API → managed inputs → durable queue |
| 17 Local kill | SDK/API cancellation and `fl_agent.board_worker` hardware cleanup | `test_queue.py`, `test_agent.py`, `test_websocket_execution.py`: queued cancellation preserves active worker; running cancellation stops hardware |
| 18 Shmoo | `fl_agent.shmoo` with bounded discrete voltage/frequency axes | `test_shmoo.py`: baseline failure aborts, no float accumulation, every attempted sample retained |
| 19 Scheduler database | `fl_scheduler.db`, frozen Alembic revisions 0001–0010 | `test_migrations.py`, `test_database_cancellation.py`: actual PostgreSQL upgrades, schema contracts and canceled database work |
| 20 Placement | `fl_scheduler.scheduler.placement`, shared resource matching | `test_scheduler.py`: explicit board/resource matches, nine-board placement and incompatible/stale exclusion |
| 21 Queue cost | `fl_common.scheduling`, scheduler placement | `test_queue.py`, `test_scheduler.py`: per-job capped timeout costs influence real placement |
| 22 Priority | Agent transactional priority/FIFO queue, placement ordering | `test_queue.py`, `test_scheduler.py`, `test_scheduler_load.py`: niceness order and stable equal-priority FIFO |
| 23 Reservations | Scheduler creation/placement and durable reservations | `test_scheduler.py`, `test_unregister_reservations.py`: concurrent claims, bounded expiry, no duplicate booking or unacknowledged dispatch reclamation |
| 24 Protocol | Versioned `fl_common.models.events`/`protocol`; agent connection and scheduler gateway/outbox | `test_connection.py`, `test_outbox_batches.py`, `test_snapshot_backpressure.py`, `test_websocket_execution.py`: durable receipts, bounded batches and session/cursor correlation |
| 25 Reconciliation | Agent events/snapshots; scheduler reconcile and session fencing | `test_network_partitions.py`, `crash/test_scheduler_restart.py`: offline completion, queued cancellation and pending-command replay reconcile once |
| 26 Heartbeats | Agent connection; registry health and default maintenance | `test_scheduler.py`, `test_network_partitions.py`: stale cluster exclusion and OFFLINE health without interrupting hardware |
| 27 Remote client | `fl_client.main`, `api`, `monitor`, `results` | `test_remote_client.py`, `test_remote_monitor.py`, `test_terminal_submission.py`: all required commands, bounded live logs and terminal state/results |
| 28 Twelve-step submission | Client receipts/submission, scheduler upload/delivery workers, gateway grants, Mac fetch/verify/enqueue | `test_terminal_submission.py`, `test_private_terminal.py`, `test_transfer_delivery.py`: bitstream+ELF, independent SHA checks, durable ACK before enqueue and lost-response resume without duplicate execution |
| 29 Terminal authentication | `fl_client.auth`/`session`; scheduler login/sessions | `test_authentication.py`, `test_submission_login.py`: URL/code approval, missing/expired session login, rotating/revocable protected credentials |
| 30 Groups/roles | Google verifier, delegated Directory adapter, bounded membership cache and owner/role checks | `test_google_verification.py`, `test_authentication.py`, `test_public_api.py`: non-member denial, issuer/audience/nonce validation and provider-outage denial; external providers injected |
| 31–32 Private network/nodes | Headscale config/policy, adapter and deployment renderer; cluster-specific tags | `test_private_network.py`, `test_private_bbcp.py`: real peers, cluster isolation, bounded ports, verified relay clients and source-fenced SSH, under normal and forced DERP paths |
| 33 Enrollment | Admin tickets, scoped pre-auth keys, registration verification, encrypted replay receipts and revocation worker | `test_enrollment.py`, `test_headscale_adapter.py`, `test_macos_enrollment.py`, `test_retirement_retries.py`: role boundaries, enrollment races, lost replies and durable cleanup |
| 34 License relay (optional per user correction) | Opt-in `license_relay` mapping; dedicated tag, fixed-port HAProxy and matching ACL/private DNS; omitted/null emits no relay files, license DNS or ACL/tag | `test_deployment_native.py`, `test_deployment_relay.py`: native parser and real two-port forwarding with denied interfaces/ports; actual BWRC checkout/release remains untested when enabled |
| 35 Local status/TUI | `fl_cli.dashboard`, inventory editor and SDK snapshots | `test_dashboard.py`, `test_dashboard_views.py`, `test_inventory_editor.py`: tables, metrics, details, selection, stale reads and 80×24 interaction |
| 36 Scheduler UI | React cluster/board/job and admin enrollment/user routes; same-origin scheduler API | Six frontend tests; `test_scheduler_dashboard.py` with Chromium/PostgreSQL/HTTPS: all seven routes/reloads, owner/admin denial, polling, logout, enrollment secrecy and desktop/mobile layouts |
| 37 Notifications | Durable event projection/delivery, Mailgun/Slack/Google Chat adapters and warning events | `test_notification_projection.py`, `test_notification_delivery.py`, `test_notification_alerts.py`, `test_notification_admin.py`: provider envelopes/ACKs, fencing/retry, once-per-event fanout and bounded expiry warnings; no external messages sent |
| 38 Security | Strict YAML models, protected secret files, cookie/session roles, fixed artifact paths, scoped SSH/BBCP and public/private proxy routes | Models/auth/public API/gateway scopes/transfer tests plus native nginx and browser acceptance: independent SHA, role/owner denial, replay/revocation, CSP/origin and route boundaries |
| 39 Testing strategy | Unit, mock hardware, three-agent/nine-board integration, SIGKILL and real TCP partition fixtures | Current full run; separate 10/60/600-second run; scheduler fresh-process and management/deletion/retirement crash tests |
| 40 Milestones | All fifteen software milestones mapped in the requirement ledger | Evidence above and ledger; physical firmware and actual smart-plug control are outside the initial software scope |
| 41 Architecture rules | SDK/API separation; local worker authority; ephemeral cache/durable queue; public client stays outside Headscale | Terminal/private transport, crash/partition/restart, scope/owner tests and browser request-origin checks; scheduler does not dispatch hardware through SSH |
| 42 MVP | Terminal client → authenticated placement/reservation → native BBCP → Mac verification → mock hardware → logs/results | `test_terminal_submission.py`, `test_private_terminal.py`: complete workflow with user host outside private network, including resumes and retained inputs/results |
| 43 Final system | Separate scheduler, agent, client, gateway, Headscale and license-relay components, documented deployment bundles | Combined native-network acceptance and deployment parser/runtime tests; production installation is not performed here |

Tests named without a prefix are in `tests/integration` or `tests/unit`; deployment tests are
in `tests/`. This keeps the table readable without duplicating test contents.

## Operational validation remaining

These are environment-dependent checks, not implementations silently omitted from the plan:

1. On an actual Mac, install the locked environment and validate launchd registration/recovery,
   protected Unix sockets, native Tailscale DNS/TUN/TLS, sleep/wake and reboot behavior, Darwin
   BBCP, and the local terminal UI. Follow [Mac operations](macos-operations.md).
2. Supply the actual firmware operation mappings and validate board power/program/run/stop,
   UART, sensor and shmoo behavior. The configurable `LilikoiBoardBackend` is implemented;
   the separately supplied firmware is not fabricated.
3. Install the rendered Linux bundles and validate actual systemd boot, certificate issuance,
   public/private listeners, production HTTPS DERP and real NAT/firewall traversal. Linux
   userspace transport adapters are test fixtures and are not evidence of native Mac networking.
4. Configure Google Workspace OAuth/delegated Directory groups and notification destinations,
   then perform live acceptance. Current tests verify those boundaries with injected providers.
5. Only if license forwarding is enabled, obtain authorized BWRC manager/vendor endpoints, fixed
   ports, hostname behavior and license
   entitlement. Verify checkout/release, relay restart and denied unrelated BWRC access as
   described in [license relay operations](license-relay.md). No broad subnet access is added.

An intermittent public BBCP upload timeout remains unexplained. It occurred in earlier repeated
acceptance; subsequent repetitions and the current full suite pass. A distinct, reproduced
concurrent receiver bind failure was fixed with bounded per-port OS locks and verified with
eight independent clients under both narrow and default ranges. That fix is not evidence that
the unexplained timeout is resolved. Preserve this issue during deployment acceptance; see
the requirement ledger and [transfer gateway operations](transfer-gateway.md).
