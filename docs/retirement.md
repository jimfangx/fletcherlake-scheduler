# Permanent Mac retirement

`fl cluster destroy` drains the local daemon, interrupts running jobs, cancels its durable
queue, and finalizes collateral expiry before unregistering. A failed board shutdown prevents
retirement. The final snapshot also lets the scheduler cancel reservations that never reached
the Mac. An omitted dispatched/queued job blocks unregister rather than silently losing it.

The scheduler commits final metadata, disables the cluster, fences its WebSocket session, and
queues node revocation in one PostgreSQL transaction. Board IDs identify physical boards
across enrollments. A new enrollment can claim them only after acknowledged unregister, with
no active board or unfinished assignment. Old jobs and assignments keep their original cluster
UUID; `BOARD_REASSIGNED` events record the physical ownership change. An authentication check
that raced retirement cannot open another session or change retired inventory.

## Retry and archival

Before teardown, a mode-0600 `pending-destroy.json` records the final snapshot and installation
in `/Library/Application Support/fl-retired`. One OS-held management lock serializes lifecycle
operations. Init, confirm, reconfigure, and restart reject a pending destroy; repeat destroy to
finish it. Replays use the saved snapshot and stop contacting the daemon after its shutdown.
Missing credentials block unregister and teardown; restore the protected agent credentials
before retrying. They are never treated as evidence that the scheduler accepted retirement.

| Journal phase | Durable evidence | Recovery |
| --- | --- | --- |
| `DRAINED` | Hardware drain returned a final snapshot | Retry authenticated unregister; the agent remains installed |
| `UNREGISTERED` | Scheduler accepted final metadata | Finish native shutdown/logout; independent cleanup can sweep a stopped pending state directory |
| `LOGGED_OUT` | Native daemon teardown/logout finished and its plist was removed | Finish atomic archival; the retention entrypoint can complete this after process death |

New enrollment credentials store the public scheduler origin separately from the private
agent origin. Unregister uses `/api/enrollment/clusters/<UUID>/unregister` over public HTTPS,
authenticated only by that cluster's agent token. Human sessions, bootstrap tickets, and other
agents cannot use it. This single action remains reachable if the unregister response is lost
and node revocation already completed. Agent WebSocket and other control endpoints remain
private. Legacy credentials can record the public origin explicitly before retirement:

```sh
pixi run fl cluster destroy --scheduler https://scheduler.example.edu
```

The CLI verifies that the installed launchd definition and local socket belong to the selected
cluster. After acknowledgement it stops launchd and holds both daemon locks until archival,
preventing writes during the SQLite checkpoint and directory move. It logs out Tailscale,
removes credentials, bootstrap receipts, temporary join keys and confirmation, then atomically
renames the entire active state directory:

```text
/Library/Application Support/fl-retired/
├── management.lock
├── latest.json
├── logs/
└── <retired-cluster-UUID>/
    ├── cluster.yaml
    ├── snapshot.json
    ├── agent.db
    ├── agent.lock
    ├── jobs/
    └── logs/
```

The final snapshot is immutable; subsequent collateral deletions are recorded in archived
SQLite events and artifact rows. `latest.json` binds repeat destroy calls to the last completed
retirement when no active state exists. The journal and directory parents are fsynced. A kill
before or after rename can be recovered without a hardware process. Files are never copied
into a fresh cluster database, and there is no automatic replay of retired jobs.

A later `setup init` uses a new enrollment ticket and UUID in a fresh `fl` directory. Old
history remains in its archive. Existing databases left by older implementations without a
retirement journal are refused by init when credentials are absent; they require explicit
operator recovery and cannot be silently attributed to a new cluster.

## Independent retention

Destroy checks the locked Pixi runtime and installs
`edu.berkeley.fletcherlake.retention.plist` before unregistering. The job calls `fl-retention`
with the archive root, uses `RunAtLoad` and a 300-second `StartInterval`, and retains the same
stable project/Pixi location as the agent. Those are native
[launchd scheduling fields](https://github.com/apple-oss-distributions/launchd/blob/main/man/launchd.plist.5).
Keep that checkout and Pixi installed while retained collateral remains. A sleeping or powered
off Mac cleans overdue data on a subsequent invocation; SQLite expiry is the authority.

The maintenance entrypoint imports no hardware service or networking client. It opens each
protected, UUID-bound archive under an exclusive lock, verifies terminal state and completed
retention, and uses the existing durable deletion records and UTC expiry timestamps. A crash
between file removal and the deletion commit retries safely. A corrupt archive reports failure
without exposing its contents or blocking other archives. Concurrent cleanup skips held locks.
Symlinked archive metadata or collateral roots are rejected. Job metadata/events remain after
collateral expiry; system logs have no automatic expiry policy in this implementation.

```sh
pixi run fl-retention --retired-root '/Library/Application Support/fl-retired' --check
pixi run fl-retention --retired-root '/Library/Application Support/fl-retired'
```

Retired collateral is retained locally; the retired node no longer serves remote transfers.
For custom state roots, the archive root is the adjacent directory with `-retired` appended.
Python uses the same flow through `ClusterSetup.destroy(scheduler=...)`. Linux acceptance uses
real agent workers/PostgreSQL and SIGKILL recovery with native commands injected; actual macOS
launchctl, reboot, sleep/wake, and firmware acceptance remain required.
