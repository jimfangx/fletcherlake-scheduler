# Fletcherlake bringup platform

This repository implements the platform described in `context/INST_Agent.md`. The Mac agent
owns physical execution and durable queues. The scheduler owns global placement and
identity; clients talk to APIs rather than controlling hardware.

The plan's software components are implemented, including macOS provisioning and launchd,
the durable local agent, hardware adapters, local SDK/CLI/TUI, PostgreSQL scheduling,
authenticated agent WebSockets, Google authentication/Groups authorization, terminal login
approval, and Linux deployment configuration. Linux acceptance uses mock boards and injected
external identity/notification providers; actual Mac and live service validation remains pending.
The remote client supports terminal login, submission/resume, retained results,
live log reads, state following, job listing/status/cancel, and logout. Administrator enrollment, retryable Mac setup, and durable
network revocation are implemented. Permanent retirement archives history, continues collateral
cleanup through a separate launchd job, and permits fresh enrollment without replaying old work.
The local Textual dashboard has readable status tables, metrics and job details; interactive
setup/reconfiguration edits host and board inventory with schema validation and cancellation.
See [local interfaces](docs/local-interface.md).
The Headscale
adapter and shipped ACLs are tested with real userspace peers. The rclone/SFTP gateway transport,
scoped HTTP control, integrity checks, revocation, and retention are tested with real rclone and OpenSSH.
Durable scheduler delivery and Mac fetch/verification now gate execution through live WebSockets.
Combined [private-network acceptance](docs/private-network-acceptance.md) carries native rclone/SFTP,
private HTTPS/WSS and terminal submission/results through actual Headscale peers, with normal
paths and forced local DERP transport. Native Mac networking remains an operational gate.
Public reserved upload grants and protected idempotent receipts automate terminal submission.
Durable Mailgun/Slack/Google Chat consumers and execution/retention/downtime warning events are
implemented with intercepted-provider acceptance tests. The React scheduler dashboard provides
cluster/board/job views and administrator enrollment/user pages with same-origin cookie login.
See [dashboard build and operation](docs/scheduler-dashboard.md) and
[the requirement audit](docs/requirement-audit.md) for source/test traceability and deployment
validation still required. The [requirement ledger](docs/implementation-status.md) preserves
historical regression evidence, including a prior BBCP upload timeout. The current transport
uses rclone/SFTP; follow the [migration instructions](docs/transfer-gateway.md#migrating-an-existing-bbcp-deployment)
when upgrading an existing deployment.
The reviewed Linux deployment renderer keeps explicit listener addresses, private DNS,
ACL ports and pinned SSH endpoints consistent. See [Linux deployment](docs/linux-deployment.md).

## Development

Install [Pixi](https://pixi.sh), then run:

```sh
pixi install --locked
pixi run test
pixi run lint
pixi run typecheck
```

The lockfile covers Linux x86-64 and macOS Apple Silicon, with Python 3.12. Python source is
split into `fl-common`, `fl-agent`, and `fl-cli` directories. One platform distribution packages
these modules and service packages together for now; this avoids independent version drift in
shared contracts.
Production services can later become separate distributions without changing their import APIs.

Tests run entirely with mock boards and a disposable real PostgreSQL server. Socket access is
required: the daemon uses a Unix socket,
and asyncio uses local socket pairs for thread completion. Sandboxes that block socket sends
cannot run the agent integration suite correctly.

## Simulate an agent on Linux or macOS

Set up a temporary state directory using the explicit mock inventory:

```sh
pixi run python examples/init_simulation.py --state-root /tmp/fl-demo
pixi run fl-agent --state-root /tmp/fl-demo --runtime-root /tmp/fl-demo/run
```

In another terminal, set the socket location, create your input files, and submit a job:

```sh
export FL_AGENT_SOCKET=/tmp/fl-demo/run/agent.sock
pixi run fl cluster status
pixi run fl cluster status --dashboard
pixi run fl job submit examples/job.yaml
pixi run fl job status JOB_UUID
pixi run fl job kill JOB_UUID
```

Paths in `job.yaml` are resolved relative to that file. The example expects files at
`examples/build/hello.elf` and `examples/build/top.bit`; supply your own inputs. Mock boards do
not execute the ELF, but exercise programming, execution, results, UART, and retention paths.
Every board must explicitly declare `backend: mock` or `backend: lilikoi`.

The local Python API calls the same daemon directly:

```python
from pathlib import Path
from fl import Cluster

with Cluster.local(Path("/tmp/fl-demo/run/agent.sock")) as cluster:
    job = cluster.submit("examples/job.yaml")
    print(cluster.job(job.spec.job_id).state)
```

`fl.ClusterSetup` shares provisioning and lifecycle workflows with the CLI. See the
[macOS Python management examples](docs/macos-operations.md#python-management).

## Mac deployment

See [macOS operations](docs/macos-operations.md). Lifecycle commands deliberately require
macOS; the Linux scheduler host will not use launchd. Tests validate the generated launchd
contract and failure paths on Linux, but actual installation and reboot require a Mac.

The optional Kasa fields and force-off interface are scaffold-only. There is no power-on API
and no dependency on smart-plug software.

## Scheduler deployment

Start with the [complete deployment guide](docs/deployment-guide.md) for all components,
Google Workspace credentials, AWS/GCP hosting and optional third-party integrations.

See [Linux scheduler operations](docs/scheduler-operations.md) and
[authentication](docs/authentication.md) for Google Workspace configuration, secret injection,
database migrations, public API behavior, and the remaining integration boundaries.
See [Headscale operations](docs/headscale-operations.md) for enrollment, private listeners,
network acceptance tests, and the [optional BWRC license relay](docs/license-relay.md).
For Vivado Lab on the Macs, leave `license_relay` omitted or `null` in the deployment inventory;
no relay host or BWRC license access is required.

See [the remote client](docs/remote-client.md) for terminal login and implemented monitoring
and submission/resume commands. See [the transfer gateway](docs/transfer-gateway.md) for protocol,
deployment, and transport acceptance. See [live monitoring](docs/live-logs.md) for bounded
log reads, resume offsets, retention checks, and state following.
See [notifications](docs/notifications.md) for protected provider setup, delivery guarantees,
warning policy, retries, and administrator inspection.
