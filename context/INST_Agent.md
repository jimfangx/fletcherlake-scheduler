
# Remote Silicon Bringup Platform — Implementation Plan

## 0. Goal

Build the software infrastructure for a remote silicon bringup platform consisting of:

- multiple Mac Minis;
- up to three custom PCB + FPGA + SoC platforms attached to each Mac Mini;
- a local daemon/CLI/TUI/Python API on each Mac Mini;
- a central scheduler;
- a web dashboard;
- a terminal-only remote submission client that can run from arbitrary Chipyard machines;
- a Headscale-managed private network connecting infrastructure machines;
- durable queues, job state, artifact retention, scheduling, authentication, and monitoring.

The PCB firmware itself is outside the scope of this project and will be provided separately. The Mac-side software therefore needs a clean abstraction layer over whatever shell/Python commands ultimately control power, FPGA flashing, SoC loading, UART, JTAG, clocks, sensors, etc.

The high-level authority split should be:

```text
Scheduler:
    global scheduling
    authentication / authorization
    cluster inventory
    reservations
    web UI
    job metadata
    notifications

Mac Mini fl-agent:
    authoritative local hardware state
    per-board queues
    execution
    collateral
    crash recovery

fl-client:
    remote terminal workflow
    authentication
    submission
    transfer orchestration
    job monitoring

Headscale:
    private infrastructure network

Transfer Gateway:
    bridges arbitrary public/non-VPC machines
    to private Mac Minis

Google Groups:
    human authorization source
```

---

# 1. Target Architecture

```text
                        PUBLIC INTERNET

 Chipyard / user machine
 ┌──────────────────────┐
 │ fl-client            │
 │                      │
 │ login                │
 │ submit job.yaml      │
 │ status               │
 │ logs                 │
 │ results              │
 └──────────┬───────────┘
            │ HTTPS
            ▼

 ┌────────────────────────────────────────────────────┐
 │ Scheduler Server                                   │
 │                                                    │
 │ Scheduler REST API                                 │
 │ Scheduler Web UI                                   │
 │ OAuth                                              │
 │ Google Groups authorization                        │
 │ Scheduler Core                                     │
 │ PostgreSQL                                         │
 │ Agent WebSocket Gateway                            │
 │ Headscale                                          │
 └───────────────┬───────────────────────┬────────────┘
                 │                       │
                 │ Headscale             │ public transfer
                 │                       │
                 │                ┌──────▼──────────────┐
                 │                │ Transfer Gateway    │
                 │                │ BBCP staging        │
                 │                └──────┬──────────────┘
                 │                       │
                 │                       │ Headscale
                 │                       │
       ┌─────────▼───────────────────────▼──────┐
       │ Mac Mini                               │
       │                                        │
       │ fl-agent                               │
       │ SQLite                                 │
       │                                        │
       │ BoardWorker 0 ───── PCB/FPGA/SoC 0     │
       │ BoardWorker 1 ───── PCB/FPGA/SoC 1     │
       │ BoardWorker 2 ───── PCB/FPGA/SoC 2     │
       └────────────────────────────────────────┘
```

Optional future out-of-band shutdown:

```text
Scheduler
    │
    │ future implementation
    ▼
Kasa smart plug
    │
    X power
    │
Mac Mini
```

The Kasa interface should be **scaffolded in configuration and APIs**, but it is not part of the initial implementation.

---

# 2. Repository Structure

Use a monorepo.

```text
fletcherlab/
├── pixi.toml
├── pyproject.toml
│
├── packages/
│   ├── fl-common/
│   │   ├── models/
│   │   │   ├── cluster.py
│   │   │   ├── board.py
│   │   │   ├── job.py
│   │   │   ├── artifact.py
│   │   │   └── events.py
│   │   ├── protocol/
│   │   └── errors.py
│   │
│   ├── fl-agent/
│   │   ├── daemon.py
│   │   ├── db.py
│   │   ├── board_worker.py
│   │   ├── executor.py
│   │   ├── collateral.py
│   │   └── hardware/
│   │       ├── base.py
│   │       ├── mock.py
│   │       └── lilikoi.py
│   │
│   ├── fl-cli/
│   │   ├── cluster.py
│   │   ├── job.py
│   │   ├── tui/
│   │   └── sdk/
│   │
│   └── fl-client/
│       ├── auth.py
│       ├── submit.py
│       ├── status.py
│       └── bbcp.py
│
├── services/
│   ├── scheduler/
│   │   ├── api/
│   │   ├── scheduler/
│   │   ├── agents/
│   │   ├── notifications/
│   │   └── db/
│   │
│   └── transfer-gateway/
│
├── web/
│   └── scheduler-ui/
│
└── tests/
    ├── unit/
    ├── integration/
    ├── crash/
    └── e2e/
```

Recommended stack:

```text
Python 3.12+
Pydantic
Typer
Textual
FastAPI
SQLAlchemy
Alembic
PostgreSQL
SQLite WAL
WebSocket / Socket.IO
React / TypeScript
Pixi
```

Use Python for the agent, scheduler backend, CLI, SDK, and remote client so schemas and APIs can be shared.

---

# 3. Shared Schemas

Implement shared schemas before writing scheduler or hardware logic.

Every major structure should contain:

```text
schema_version
```

because PCB capabilities and scheduler functionality will evolve.

---

## 3.1 ClusterConfig

The cluster configuration should describe the Mac Mini and all attached hardware as required by the original design.

Conceptually:

```python
class ClusterConfig(BaseModel):
    schema_version: int
    cluster_id: UUID | None

    boards: list[BoardConfig | None]

    environment: EnvironmentConfig

    os: OSInfo
    apple_model: str
    mac_address: str

    power_control: PowerControlConfig | None = None
```

A Mac can have fewer than three boards.

Example:

```python
boards = [
    BoardConfig(...),
    BoardConfig(...),
    None,
]
```

---

## 3.2 BoardConfig

Example:

```python
class BoardConfig(BaseModel):
    board_id: str

    device_mapping: dict[str, str]

    num_vrails: int
    num_vsense: int
    num_isense: int

    clock_source: Literal[
        "on_chip",
        "fpga",
        "external_clock_gen"
    ]

    fpgas: list[FPGAConfig]
    socs: list[SoCConfig]
```

SoC names should match names used by other tooling such as the Baremetal IDE to prevent naming inconsistencies.

---

# 4. Optional Kasa Power-Control Scaffold

Add optional power-control fields now so configuration compatibility does not need to change later.

This functionality does **not** need to actually work in the first implementation.

Example:

```python
class PowerControlConfig(BaseModel):
    type: Literal["kasa"]

    device_id: str | None = None
    host: str | None = None
    alias: str | None = None
```

And:

```python
power_control: PowerControlConfig | None = None
```

Example YAML:

```yaml
power_control:
  type: kasa
  device_id: lab-macmini-01
  host: 192.168.50.24
  alias: Fletcherlake Mac Mini 01
```

Or simply:

```yaml
power_control: null
```

for clusters without one.

### Initial implementation requirement

Only implement:

- schema;
- database fields;
- scheduler model representation;
- optional API/interface abstraction;
- UI/config placeholders if useful.

Do **not** require:

- `python-kasa`;
- device discovery;
- network communication to the plug;
- actual shutdown commands.

Have an interface such as:

```python
class ClusterPowerController(Protocol):
    async def force_power_off(
        self,
        cluster: ClusterConfig
    ) -> None:
        ...
```

but the initial implementation may use:

```python
class UnsupportedPowerController:
    async def force_power_off(...):
        raise NotImplementedError
```

This leaves a clear integration point.

---

## 4.1 Future Kasa semantics

When implemented later, certain authenticated operator/admin users may be able to forcefully shut down a cluster.

Possible permission:

```text
cluster:force_poweroff
```

The eventual path would be:

```text
authorized operator
        │
        ▼
scheduler
        │
        ▼
Kasa device
        │
        X power
        │
Mac Mini
```

This is specifically an emergency out-of-band kill.

### There is no remote power-on operation

Do **not** define:

```text
cluster:poweron
```

and do not create:

```text
POST /clusters/:id/power-on
```

The Kasa integration, if implemented, is intentionally one-way from the scheduler's perspective:

```text
ON
 ↓
force off
 ↓
OFF
```

Restoration requires physical/manual intervention outside this scheduler.

The scheduler should therefore consider a Kasa force-off terminal from its own perspective:

```text
FORCE_POWER_OFF_PENDING
    ↓
POWERED_OFF
```

and should not attempt automatic recovery.

---

# 5. JobConfig vs JobSpec

Users should submit a **JobConfig**.

The system constructs a **JobSpec** by combining user-requested configuration with trusted metadata.

Example user file:

```yaml
priority: 0

resource_constraints:
  board: fletcherlake-0
  soc: fletcherlake
  fpga: xcvu9p

run_timeout: 3600
run_collateral_ttl: 30

binary: ./build/hello.riscv
bitstream: ./build/top.bit

shmoo: null
```

The original design includes owner, submission time, resource constraints, timeouts, collateral TTL, binaries, bitstreams, and shmoo parameters.

Do **not** trust users to populate:

```text
owner
job_id
date_submitted
assigned_board
```

Instead:

```text
JobConfig
    +
authenticated identity
    +
scheduler metadata
    =
JobSpec
```

Example:

```python
class JobSpec(BaseModel):
    job_id: UUID
    owner: str
    submitted_at: datetime

    priority: int

    resource_constraints: ResourceConstraints

    run_timeout_seconds: int
    collateral_ttl_days: int

    binary: ArtifactRef | None
    bitstream: ArtifactRef | None

    shmoo: ShmooConfig | None
```

Validation:

```text
run_timeout <= 35 days
collateral_ttl <= 365 days
step > 0
shmoo.low <= shmoo.high
```

---

# 6. Hardware Abstraction Layer

Do not directly sprinkle shell-script invocations throughout `fl-agent`.

Create a stable abstraction.

```python
class BoardBackend(Protocol):

    async def discover(self): ...

    async def power_on(self): ...
    async def power_off(self): ...

    async def set_voltage(self, rail, volts): ...
    async def read_voltage(self, channel): ...
    async def read_current(self, channel): ...

    async def set_frequency(self, hz): ...

    async def program_fpga(self, bitstream): ...
    async def program_soc(self, elf): ...

    async def start(self): ...
    async def stop(self): ...

    async def read_uart(self): ...
```

Implement:

```text
MockBoardBackend
LilikoiBoardBackend
```

Initially, `LilikoiBoardBackend` may wrap shell/Python firmware utilities.

All subprocess execution must:

```text
avoid shell=True
capture stdout
capture stderr
have timeout support
support cancellation
return structured errors
```

The firmware interface is intentionally incomplete right now, so the rest of the system should not depend on exact firmware CLI syntax.

---

# 7. Mac Mini Architecture: `fl-agent`

The persistent daemon is the authority for local hardware.

Do not make the CLI itself own hardware.

```text
fl CLI ────────┐
fl TUI ────────┤
Python SDK ────┼── local API ──> fl-agent ──> hardware
Scheduler ─────┘                   │
                                  ▼
                               SQLite
```

Run `fl-agent` using macOS `launchd`.

---

# 8. One Worker / Queue Per Board

Each board has exactly one execution worker.

```text
fl-agent
   │
   ├── BoardWorker(board-0)
   │      └── queue
   │
   ├── BoardWorker(board-1)
   │      └── queue
   │
   └── BoardWorker(board-2)
          └── queue
```

This directly implements the required one-queue-per-board model.

Each worker owns an exclusive board lock.

Nothing else may independently manipulate that board while the worker is running a job.

---

# 9. Durable Local State

Use SQLite, not in-memory queues.

Suggested filesystem layout:

```text
/Library/Application Support/fl/
├── cluster.yaml
├── cluster.sha256
├── agent.db
├── jobs/
│   └── <job-id>/
│       ├── job.json
│       ├── binary.elf
│       ├── fpga.bit
│       ├── stdout.log
│       ├── stderr.log
│       └── results.json
└── logs/
```

`agent.db` should include:

```text
jobs
board_queue
job_events
artifacts
collateral_deletion
board_state
```

Enable SQLite WAL.

Queues and collateral deletion information must survive power failure.

---

# 10. Artifact TTL Management

Do not use cron as the authoritative state store.

Persist:

```text
artifact.expires_at
```

in SQLite.

The agent runs a periodic deletion sweep.

```text
agent boot
    ↓
read SQLite
    ↓
find expires_at <= now
    ↓
delete collateral
    ↓
record ARTIFACT_DELETED event
```

This guarantees TTL handling survives crashes.

---

# 11. Job State Machine

Implement explicit states.

```text
CREATED
   │
   ▼
STAGING
   │
   ▼
QUEUED
   │
   ▼
PREPARING
   │
   ▼
RUNNING
   │
   ├───────────────┐
   ▼               ▼
SUCCEEDED         FAILED
                    │
                    └── TIMED_OUT
```

Cancellation:

```text
QUEUED/RUNNING
      │
      ▼
CANCELING
      │
      ▼
CANCELED
```

Infrastructure interruption states:

```text
INTERRUPTED
LOST
INTERRUPTED_BY_FORCE_POWEROFF
```

State transitions must be SQLite transactions.

Never infer current state solely from logs.

Each transition generates an event.

Example:

```json
{
  "seq": 19281,
  "job_id": "...",
  "type": "JOB_RUNNING",
  "timestamp": "...",
  "board_id": "...",
  "payload": {}
}
```

---

# 12. Bitstream Cache

The agent should remember what FPGA bitstream is currently loaded so unnecessary programming can be skipped. The cache must disappear on reboot/power loss.

Use:

```text
/var/run/fl/
├── board-0.bitstream.sha256
├── board-1.bitstream.sha256
└── board-2.bitstream.sha256
```

or clear the fingerprints every time `fl-agent` initializes.

Algorithm:

```python
wanted_sha = sha256(bitstream)

if force_reflash:
    program_fpga()

elif current_sha != wanted_sha:
    program_fpga()

else:
    skip()
```

Only write the fingerprint after successful programming.

Use atomic file replacement.

---

# 13. Cluster Setup

The source defines:

```text
fl cluster setup init
fl cluster setup confirm
fl cluster setup reconfigure
fl cluster restart
fl cluster destroy
```



---

## 13.1 `fl cluster setup init`

Flow:

```text
verify networking
    ↓
inspect cluster.yaml + cluster.sha256
    ↓
determine:
    NEW
    RECONFIGURING
    CONFIGURED
    ↓
autodetect environment
    ↓
merge user overrides
    ↓
collect board definitions
    ↓
validate
    ↓
write cluster.yaml
    ↓
join Headscale
    ↓
connect scheduler
    ↓
register cluster
```

Configuration precedence:

```text
explicit CLI value
    >
interactive user value
    >
autodetected value
    >
None
```

Automatically detect where reasonable:

```text
Vivado installation/version
OpenOCD
OpenFPGALoader
RISC-V toolchain
GCC
Clang
Chipyard
OS
Apple model
MAC address
```

Do not automatically invent board configuration.

---

## 13.2 `fl cluster setup confirm`

Flow:

```text
validate config
    ↓
acquire sudo
    ↓
pixi install/sync
    ↓
restrict config permissions
    ↓
generate cluster.sha256
    ↓
start/restart fl-agent
    ↓
report READY
```

---

## 13.3 `fl cluster setup reconfigure`

Immediately:

```text
cluster state = RECONFIGURING
rm cluster.sha256
```

Then edit via:

```text
CLI flags
interactive TUI
freeform editor
```

The missing SHA functions as a persistent indication that configuration was being edited when the machine stopped.

On success:

```text
validate
pixi sync
write SHA
reload agent
update scheduler
READY
```

On boot, if:

```text
cluster.yaml exists
cluster.sha256 absent
```

then:

```text
CONFIGURATION_INCOMPLETE
```

and no jobs should start.

---

# 14. Graceful Cluster Restart

`fl cluster restart` is the normal local restart path.

Before reboot:

```text
cluster state → DRAINING

stop accepting jobs

running job:
    stop safely
    mark INTERRUPTED

power down PCB/FPGA/SoC through firmware

scheduler:
    CLUSTER_RESTARTING

sync SQLite

cluster state → RESTARTING

reboot
```

After reboot:

```text
fl-agent starts

ephemeral bitstream cache is absent

load durable SQLite

reconnect scheduler

reconcile queues

cluster → READY
```

Queued jobs remain queued.

A previously running job must **not** automatically resume.

---

# 15. Future Force-Off Scaffold

The scheduler data model should leave room for:

```text
Force Power Off Cluster
```

for users with a permission such as:

```text
cluster:force_poweroff
```

but do not implement the actual Kasa action in the initial milestone.

Possible future API:

```text
POST /api/clusters/{cluster_id}/force-poweroff
```

If the cluster has:

```text
power_control = null
```

it should be unavailable.

For now this route may:

- not exist;
- be feature-flag disabled; or
- return `501 Not Implemented`.

The important requirement is that the architecture does not make future support difficult.

When implemented eventually:

```text
running job
    → INTERRUPTED_BY_FORCE_POWEROFF

queued jobs
    → retained

cluster
    → POWERED_OFF
```

There is deliberately **no corresponding power-on feature**.

---

# 16. `fl job submit`

The job configuration file is the primary job interface.

```bash
fl job submit job.yaml
```

Example:

```yaml
priority: 0

resource_constraints:
  board: fletcherlake-0
  soc: fletcherlake
  fpga: xcvu9p

run_timeout: 3600
run_collateral_ttl: 30

binary: ./build/test.elf
bitstream: ./build/top.bit

shmoo: null
```

Internally:

```text
fl job submit job.yaml
        │
        ▼
parse JobConfig
        │
        ▼
resolve paths
        │
        ▼
validate
        │
        ▼
local fl-agent API
        │
        ▼
hash/copy collateral
        │
        ▼
construct canonical JobSpec
        │
        ▼
SQLite transaction
        │
        ▼
enqueue BoardWorker
        │
        ▼
notify scheduler
```

For convenience:

```bash
fl job submit job.yaml --board board-1
```

may override a config field.

Precedence:

```text
CLI override
    >
JobConfig
    >
default
```

You may support shorthand:

```bash
fl job submit \
    --board fletcherlake-0 \
    --binary test.elf \
    --bitstream top.bit \
    --timeout 3600
```

but internally this must first construct the same `JobConfig`.

There should only be one real implementation path.

---

# 17. `fl job kill`

Use job UUIDs.

```bash
fl job kill <job-uuid>
```

Never use queue positions as stable identifiers.

For queued job:

```text
QUEUED
    ↓
atomically remove queue entry
    ↓
CANCELED
```

For running job:

```text
RUNNING
    ↓
hardware_backend.stop()
    ↓
hardware cleanup/reset
    ↓
CANCELED
```

Report state change to scheduler.

---

# 18. Shmoo Logic

The source wording mixes "binary search" with stepping.

Make search behavior a pluggable interface.

```python
class ShmooStrategy(Protocol):
    async def execute(...): ...
```

Initial interpretation:

## Voltage

Create discrete candidate values from:

```text
low
high
step
```

Example:

```text
0.70
0.75
0.80
0.85
0.90
```

Test the highest voltage first.

If the binary does not run:

```text
BINARY_FAILED_TO_RUN
```

and abort the sweep.

Otherwise binary search the candidates for:

```text
minimum passing voltage
```

## Frequency

Create discrete frequency candidates.

Verify the low point works.

Then search for:

```text
maximum passing frequency
```

Store every sampled point.

Example result:

```json
{
  "minimum_passing_voltage": 0.75,
  "maximum_passing_frequency": 250000000,
  "samples": []
}
```

Do not implement a 2-D V/F optimization unless explicitly specified later.

---

# 19. Scheduler Database

Use PostgreSQL.

Core tables:

```text
users
clusters
boards
agent_sessions
jobs
job_assignments
artifacts
events
notifications
```

The agent is authoritative for actual local state.

The scheduler maintains a replicated/indexed view for global decisions and dashboards.

The original design explicitly says the web dashboard should use data from the Mac-side interface rather than independently recomputing local state.

---

# 20. Scheduler Placement

## Explicit board

If:

```yaml
resource_constraints:
  board: fletcherlake-2
```

then:

```text
find requested board
verify compatibility
verify cluster available
reserve board
```

---

## Resource-based placement

If no explicit board:

```python
eligible = [
    board
    for board in online_boards
    if matches_soc(board, request.soc)
    and matches_fpga(board, request.fpga)
]
```

Then rank boards.

The original design calls for selecting a suitable board based on board/SoC/FPGA constraints and choosing among eligible queues.

---

# 21. Abuse-Resistant Queue Cost

Do not rank queues only using user-provided timeout.

Users may put:

```text
5 weeks
```

even when jobs normally take minutes.

Use both:

```text
number of queued jobs
```

and:

```text
capped estimated runtime
```

Example:

```text
queue_score =
      A * queued_job_count
    + B * capped_runtime
```

Where:

```text
capped_runtime =
    min(running_remaining_estimate, RUNNING_CAP)
    +
    Σ min(job.estimated_runtime, QUEUED_JOB_CAP)
```

Initial values can be configurable, for example:

```text
RUNNING_CAP = 6 hours
QUEUED_JOB_CAP = 2 hours
```

Simple starting formula:

```python
score = (
    queued_job_count * JOB_COUNT_WEIGHT
    + capped_runtime_seconds / RUNTIME_NORMALIZATION
)
```

For example:

```text
JOB_COUNT_WEIGHT = 10
RUNTIME_NORMALIZATION = 3600
```

Long term:

```text
estimated runtime =
    historical runtime for similar job
    if available
    else capped requested timeout
```

This avoids letting timeout abuse dominate placement.

---

# 22. Scheduling Priority

Treat priority separately from board queue cost.

Suggested semantics:

```text
smaller priority/niceness number
    =
higher scheduling priority
```

Within an equivalent priority:

```text
older submission first
```

For initial implementation:

```text
ORDER BY priority ASC, submitted_at ASC
```

More advanced fair-share policy can be added later.

---

# 23. Reservations

Scheduling must reserve a board before large artifact transfer begins.

Otherwise multiple submitters can race for the same resource.

Create:

```text
JobAssignment:
    job_id
    board_id
    state = RESERVED
    expires_at
```

For example:

```text
expires_at = now + 10 minutes
```

State:

```text
RESERVED
   │
   ├── staging starts → continue
   │
   └── timeout → EXPIRED
```

An expired reservation releases the board.

---

# 24. Agent ↔ Scheduler Protocol

Each Mac keeps one long-lived outbound WebSocket/Socket.IO connection to the scheduler. This matches the full-duplex requirement in the original design.

Agent → Scheduler:

```text
HELLO
REGISTER_CLUSTER
HEARTBEAT
STATUS_SNAPSHOT

JOB_QUEUED
JOB_STARTED
JOB_LOG
JOB_FINISHED
JOB_FAILED

ARTIFACT_EXPIRING
ARTIFACT_DELETED

CLUSTER_RESTARTING
CLUSTER_RECONFIGURING
CLUSTER_DESTROYING
```

Scheduler → Agent:

```text
REGISTER_ACK

JOB_STAGE
JOB_ENQUEUE
JOB_CANCEL

STATUS_REQUEST

CLUSTER_DRAIN
```

Every command includes:

```text
message_id
protocol_version
timestamp
```

Scheduler commands need acknowledgement:

```text
ACK(message_id)
```

All scheduler commands should be idempotent.

---

# 25. Reconnection / Reconciliation

Explicitly support:

```text
network failure
scheduler restart
Mac restart
Headscale restart
```

When agent reconnects:

```text
HELLO

cluster config/version

active job

board queue snapshots

highest event sequence number
```

Scheduler compares its own state.

Reconcile using:

```text
job UUID
```

never queue indices.

The Mac is authoritative regarding whether a physical board is currently executing a job.

---

# 26. Heartbeats

Send roughly every:

```text
5–10 seconds
```

Example:

```json
{
  "cluster_id": "...",
  "cpu": 0.23,
  "memory": 0.61,
  "disk_free": 19382939232,
  "board_states": [],
  "active_jobs": []
}
```

Cluster status progresses:

```text
ONLINE
    ↓
DEGRADED
    ↓
OFFLINE
```

Do not terminate a hardware job merely because a WebSocket connection briefly disappears.

---

# 27. Remote Submission: `fl-client`

Users must be able to perform the complete job workflow from a terminal on an arbitrary Chipyard machine.

They should **not** need:

```text
sudo
Headscale membership
Tailscale membership
scheduler web UI
direct access to Mac Minis
```

This is a major requirement from the original design.

Primary commands:

```bash
fl-client login

fl-client submit job.yaml

fl-client jobs

fl-client status <job-id>

fl-client logs <job-id>

fl-client cancel <job-id>

fl-client results <job-id>
```

Optional:

```bash
fl-client submit job.yaml --follow
```

---

# 28. Remote Submission Flow

User runs:

```bash
fl-client submit job.yaml
```

Everything below should happen automatically.

## Step 1 — parse JobConfig

```text
job.yaml
   ↓
Pydantic validation
```

Resolve collateral paths.

Calculate:

```text
size
SHA-256
```

for each artifact.

---

## Step 2 — authenticate

Use cached authentication if valid.

If not:

```text
fl-client login
```

is automatically initiated.

---

## Step 3 — send lightweight JobConfig

Send:

```text
resource requirements
timeout
TTL
priority
shmoo
artifact metadata
artifact SHA
artifact size
```

Do not upload multi-gigabyte bitstreams through the scheduler API.

---

## Step 4 — scheduler reserves board

```text
resource matcher
    ↓
queue scorer
    ↓
reservation
```

---

## Step 5 — scheduler returns staging credentials

Example:

```json
{
  "job_id": "...",
  "transfer_gateway": "...",
  "staging_token": "...",
  "expires_at": "..."
}
```

---

## Step 6 — fl-client invokes BBCP

The user does not manually execute BBCP.

Conceptually:

```text
bbcp bitstream → gateway:/staging/<job-id>/bitstream

bbcp ELF → gateway:/staging/<job-id>/binary
```

The original design specifically calls for BBCP for large binaries and bitstreams.

---

## Step 7 — gateway validates artifacts

Verify:

```text
expected SHA-256
        ==
received SHA-256
```

Reject corrupted/mismatched transfers.

---

## Step 8 — gateway transfers to Mac

Through Headscale:

```text
Transfer Gateway
      │
      │ BBCP
      ▼
target Mac Mini
```

---

## Step 9 — Mac verifies SHA again

Do not rely exclusively on gateway validation.

---

## Step 10 — staged

Scheduler changes:

```text
RESERVED
    ↓
STAGED
```

---

## Step 11 — enqueue

Agent changes:

```text
STAGED
    ↓
QUEUED
```

---

## Step 12 — CLI displays status

Example:

```text
Job: 019abf41-...

Cluster: macmini-07
Board: fletcherlake-02

Uploading binary       [████████████████] 100%
Uploading bitstream    [████████████████] 100%
Verifying collateral                  OK

Queue position                          2
State                              QUEUED
```

`--follow` then displays:

```text
QUEUED
PROGRAMMING_FPGA
PROGRAMMING_SOC
RUNNING
SUCCEEDED
```

The web UI is optional for users.

---

# 29. CLI Authentication

Use Google OAuth initiated from the terminal.

Example:

```text
$ fl-client login

Authenticate with your Berkeley Google account:

https://scheduler.example.edu/device

Code: X7DF-QP2K
```

If possible, automatically open the user's browser.

Otherwise print the URL.

After successful login:

```text
~/.config/fl/auth.json
```

with:

```text
0600
```

permissions.

Subsequent submissions should normally require no manual browser interaction.

---

# 30. Google Groups Authorization

Use Google Groups as the authoritative human allowlist.

Example:

```text
silicon-bringup-users@...
```

Flow:

```text
Google OAuth identity
       │
       ▼
Google Groups membership
       │
       ├── member → authorize
       │
       └── not member → reject
```

Optionally separate:

```text
silicon-bringup-users@...
silicon-bringup-operators@...
silicon-bringup-admins@...
```

Roles:

```text
users:
    submit jobs
    inspect permitted jobs/clusters

operators:
    user capabilities
    drain cluster
    restart cluster
    eventually force-poweroff

admins:
    operator capabilities
    enroll/destroy clusters
    system administration
```

The scheduler can cache memberships briefly, but Google Groups remains authoritative.

This replaces the proposed standalone whitelist DB.

---

# 31. Headscale Networking

Use **Headscale**, not managed Tailscale.

Run it alongside the scheduler on the same server.

```text
Scheduler Server
┌──────────────────────────────────────┐
│ scheduler.service                    │
│ headscale.service                    │
│ postgresql.service                   │
└──────────────────────────────────────┘
              │
              │ WireGuard/Tailscale protocol
              ▼
       private infrastructure
```

The original design requires all Mac Minis to participate in a Tailscale-style VPC.

Headscale removes dependence on managed Tailscale device limits.

---

# 32. Headscale Nodes

Nodes may include:

```text
Scheduler
Mac Minis
Transfer Gateway
License Relay
optional future Power Controller
```

User Chipyard boxes are deliberately **not** members.

Suggested tags:

```text
tag:scheduler
tag:cluster
tag:transfer-gateway
tag:license-relay
tag:power-controller
```

Intended ACL relationships:

```text
scheduler
    → clusters

clusters
    → scheduler

transfer gateway
    → clusters

clusters
    → transfer gateway

clusters
    → license relay

scheduler
    → optional power controller
```

Avoid:

```text
cluster ↔ cluster
```

unless necessary.

---

# 33. Cluster Enrollment

Scheduler web UI:

```text
Add Cluster
```

creates:

```text
scheduler enrollment token
+
short-lived Headscale pre-auth key
```

User runs:

```bash
fl cluster setup init \
    --scheduler https://scheduler.example.edu \
    --enrollment-token ...
```

Process:

```text
validate enrollment token

obtain Headscale pre-auth credentials

join Headscale

establish agent connection

send ClusterConfig

receive stable cluster UUID
```

Use:

```text
short-lived
single-purpose
ephemeral
```

Headscale enrollment credentials.

Do not embed persistent administrative keys.

The original outline specifically expects a web-generated setup mechanism for adding a new Mac Mini.

---

# 34. License Relay

Create a separate node:

```text
tag:license-relay
```

Architecture:

```text
Mac Mini
   │
Headscale
   │
license-relay
   │
BWRC network
   │
Vivado / Cadence / Synopsys licenses
```

ACL:

```text
tag:cluster
      →
tag:license-relay:<required ports>
```

Do not expose the broader BWRC network.

The original design explicitly requires connectivity to BWRC-hosted Vivado licensing.

---

# 35. Local Status CLI/TUI

Implement:

```bash
fl cluster status

fl cluster status --dashboard
```

The dashboard reads only from the local `fl-agent`.

Tabs:

```text
Boards
Jobs
Queues
Artifacts
System
```

Example board page:

```text
Board       SoC          FPGA       State      Job
----------------------------------------------------
fl-00       chipA        VU9P       RUNNING    019...
fl-01       chipA        VU9P       IDLE       -
fl-02       chipB        VU9P       QUEUED     020...
```

Jobs:

```text
OWNER      JOB       BOARD      ELAPSED      STATE
jim        019...    fl-00      00:32:18     RUNNING
alice      020...    fl-02      --           QUEUED
```

Artifacts:

```text
OWNER      JOB       SIZE       DELETES IN
jim        019...    3.2 GB     29d 11h
```

System:

```text
CPU      27%
RAM      61%
Disk     423 / 1000 GB
```

These correspond to the local dashboard information requested in the source.

---

# 36. Scheduler Web UI

Routes/views:

```text
/clusters
/clusters/:id
/boards/:id

/jobs
/jobs/:id

/admin/clusters
/admin/users
```

Example global view:

```text
Cluster        Online     Boards    Running    Queued
------------------------------------------------------
macmini-01     ●          3         2          7
macmini-02     ●          2         1          1
macmini-03     ○          3         -          -
```

The browser should query the scheduler.

Do not have browsers talk directly to Mac Minis.

```text
browser
   ↓
scheduler
   ↓
replicated agent state
```

---

# 37. Notifications

Notifications should consume scheduler events.

Do not put Mailgun/Slack logic inside job executors.

```text
JOB_SUCCEEDED
       │
       ├── email
       ├── Slack
       └── Google Chat
```

Events:

```text
job success
job failure
job timeout
job hang
cluster downtime
artifact deletion warning
artifact deletion
```

The source requests Mailgun, Slack and Google Chat integrations.

---

# 38. Security Requirements

Implement security from the start.

## Scheduler

Externally expose:

```text
HTTPS / 443
```

Use:

```text
OAuth
Google Groups authorization
```

If IP allowlisting is desired, enforce it at:

```text
cloud firewall
load balancer
WAF
```

Note that firewall blocking does not itself prevent DNS resolution.

---

## Mac agents

Do not expose Mac agent ports publicly.

Use:

```text
Headscale network
```

and/or outbound scheduler connections.

---

## Secrets

Do not store secrets inside:

```text
cluster.yaml
job.yaml
```

Use server-side secret storage or protected OS configuration.

---

## Artifact paths

Do not trust user filenames.

Internally store:

```text
/jobs/<UUID>/binary
/jobs/<UUID>/bitstream
```

Original names can be metadata only.

---

## Artifact integrity

Verify SHA-256:

```text
client → gateway

gateway → Mac
```

---

## Authorization

Every mutable operation should verify:

```text
request.user == job.owner
OR
authorized operator/admin
```

Operations include:

```text
cancel job
delete collateral
download result
restart cluster
force power off cluster
```

---

# 39. Testing Strategy

The entire system should be developable without physical PCBs.

---

## Unit Tests

Test:

```text
config validation
JobConfig validation
JobSpec construction

queue ordering
queue scoring
priority semantics

resource matching

reservations

SHA caching

TTL handling

shmoo algorithm

state transitions

protocol serialization
```

---

## Mock Hardware Tests

Create:

```text
MockBoardBackend
```

capable of simulating:

```text
programming
execution
success
failure
timeouts
UART output
voltage behavior
frequency behavior
```

A full simulated Mac Mini should support:

```text
3 boards
```

---

## Multi-cluster Integration Test

Run:

```text
scheduler
+
3 simulated Mac Minis
+
3 boards each
```

giving nine simulated boards.

Submit large numbers of jobs.

Verify:

```text
resource placement
priority
queue scoring
reservations
cancellation
reconnects
```

---

## Crash Tests

Kill the agent using:

```bash
kill -9
```

during:

```text
QUEUED
FPGA_PROGRAMMING
SOC_PROGRAMMING
RUNNING
CANCELING
reconfiguration
artifact deletion
```

Restart and verify consistency.

---

## Network Partition Tests

Drop the scheduler connection for:

```text
10 seconds
1 minute
10 minutes
```

Running jobs should continue locally.

---

## Scheduler Restart

Restart the scheduler while jobs execute.

Agents continue executing.

After reconnect:

```text
state reconciles correctly
```

---

## Mac Restart

Verify:

```text
queued jobs survive
artifact records survive
TTL survives
bitstream fingerprint disappears
running job becomes INTERRUPTED
```

---

# 40. Implementation Milestones

Implement in this order.

---

## Milestone 0 — Repository and CI

Deliver:

```text
monorepo
Pixi
Python package layout
linting
type checking
unit-test infrastructure
shared models
```

Commands:

```text
pixi run test
pixi run lint
pixi run typecheck
```

must pass.

---

## Milestone 1 — Shared Models

Implement:

```text
ClusterConfig
BoardConfig
EnvironmentConfig
PowerControlConfig optional scaffold

JobConfig
JobSpec
ArtifactRef
ShmooConfig

protocol messages
```

Add serialization compatibility tests.

No real hardware.

---

## Milestone 2 — Hardware Abstraction + Simulator

Implement:

```text
BoardBackend

MockBoardBackend
```

Support:

```text
program FPGA
program SoC
start
stop
UART output
simulated voltage
simulated frequency
```

Exit condition:

```python
await board.program_fpga(...)
await board.program_soc(...)
await board.start()
```

works with no real PCB.

---

## Milestone 3 — Durable `fl-agent`

Implement:

```text
SQLite WAL

job state machine

board queues

BoardWorker

artifact records

TTL sweeper

bitstream SHA cache

event log
```

Exit condition:

> Killing and restarting the daemon does not lose queued jobs or artifact expiry information.

---

## Milestone 4 — Local CLI + Python API

Implement:

```text
fl cluster status

fl job submit job.yaml

fl job kill UUID

Python SDK
```

The SDK must call the agent API directly.

Do not shell out to `fl`.

Example:

```python
from fl import Cluster

cluster = Cluster.local()

job = cluster.submit("job.yaml")
```

---

## Milestone 5 — Cluster Provisioning

Implement:

```text
fl cluster setup init

fl cluster setup confirm

fl cluster setup reconfigure

fl cluster restart

fl cluster destroy
```

Implement:

```text
autodetection
config permissions
SHA reconfiguration marker
Pixi installation/sync
```

Include optional `power_control` config parsing but no real Kasa functionality.

---

## Milestone 6 — Headscale

Deploy:

```text
Headscale
```

alongside scheduler infrastructure.

Implement:

```text
cluster enrollment
pre-auth key issuance
node registration
ACL tags
```

Exit condition:

> Simulated/real Macs can register and communicate privately with the scheduler.

---

## Milestone 7 — Scheduler Core

Implement:

```text
PostgreSQL

cluster registry

board registry

agent WebSocket gateway

heartbeat tracking

resource matching

queue score

priority

reservations

state reconciliation
```

Use simulated agents.

Exit condition:

> The scheduler correctly places jobs across nine simulated boards.

---

## Milestone 8 — End-to-End Scheduler Execution

Connect:

```text
scheduler
    ↓
Mac queue
    ↓
BoardWorker
    ↓
MockBoardBackend
    ↓
result
    ↓
scheduler
```

Verify:

```text
success
failure
timeout
cancel
restart
network loss
```

---

## Milestone 9 — Authentication + Google Groups

Implement:

```text
Google OAuth

Google Groups lookup

users/operators/admin roles

scheduler authorization
```

Exit condition:

> Non-group users cannot submit jobs.

---

## Milestone 10 — Transfer Gateway + fl-client

Implement:

```text
fl-client login

fl-client submit job.yaml

fl-client jobs

fl-client status

fl-client logs

fl-client cancel

fl-client results
```

Implement:

```text
BBCP
staging credentials
artifact SHA verification
gateway-to-Mac transfer
```

Exit condition:

> A user on an arbitrary Linux/Chipyard machine with no root and no Headscale membership can submit a bitstream + ELF entirely from their terminal.

---

## Milestone 11 — Real Firmware Adapter

Implement:

```text
LilikoiBoardBackend
```

Wrap supplied firmware scripts one function at a time.

Do not modify scheduler behavior.

Do not modify JobSpec.

Do not modify queue semantics.

The only major change should be:

```text
MockBoardBackend
      ↓
LilikoiBoardBackend
```

---

## Milestone 12 — TUI + Web UI

Build:

```text
local Textual dashboard

scheduler React dashboard
```

At this point the underlying APIs should already expose all necessary data.

---

## Milestone 13 — Notifications

Add:

```text
Mailgun
Slack
Google Chat
```

as event consumers.

---

## Milestone 14 — Kasa Scaffold Completion Only

For the initial project, stop at:

```text
PowerControlConfig exists

scheduler DB stores optional fields

roles can contain future:
cluster:force_poweroff

power-control service interface exists
```

Do **not** require actual smart-plug control for project completion.

If it is implemented later:

```text
Scheduler
    ↓
Kasa
    ↓
OFF
```

only.

There is no scheduler-controlled restart or power-on sequence.

---

# 41. Important Architectural Rules

The implementation agent should treat these as hard constraints.

### 1. The CLI is not the daemon

SSH sessions may disconnect.

Hardware operation must live in:

```text
fl-agent
```

---

### 2. Do not execute jobs by SSHing from the scheduler

Use the persistent agent protocol.

```text
scheduler
    ↓ WebSocket
fl-agent
```

---

### 3. User submission machines must not join Headscale

Use:

```text
fl-client
    ↓
scheduler + transfer gateway
```

---

### 4. Job config is the canonical user-facing interface

Do not create unrelated logic for:

```text
job.yaml
```

versus:

```text
CLI flags
```

CLI flags are merely JobConfig overrides.

---

### 5. Agent state is authoritative locally

The scheduler should not guess whether a physical board is actually running.

---

### 6. Queue state must survive crashes

No in-memory-only queues.

---

### 7. Bitstream cache must not survive Mac reboot

Use ephemeral storage.

---

### 8. Artifact TTL must survive reboot

Use SQLite timestamps.

---

### 9. Scheduler must not expose private Mac Minis publicly

Use Headscale.

---

### 10. Kasa support is optional/scaffold-only initially

No current implementation dependency on `python-kasa`.

No power-on API.

---

# 42. First MVP Definition

The first meaningful end-to-end MVP is:

```text
                       ┌── board0
                       │
User → fl-client → Scheduler → Mac ── board1
                       │
                       └── board2
```

A user on a Chipyard box runs:

```bash
fl-client submit job.yaml
```

where:

```yaml
resource_constraints:
  soc: fletcherlake

binary: ./hello.riscv
bitstream: ./chip.bit

run_timeout: 3600
run_collateral_ttl: 30
```

Without:

```text
sudo
Headscale
Tailscale
manual BBCP commands
scheduler web interaction
direct Mac access
```

the system:

```text
authenticates user

checks Google Group

validates JobConfig

selects compatible board

reserves it

transfers collateral

verifies SHA

queues job

programs FPGA if necessary

programs SoC

runs binary

captures results

updates scheduler

retains collateral until TTL

returns status/results through fl-client
```

The observable state progression is:

```text
CREATED
   ↓
STAGING
   ↓
QUEUED
   ↓
PREPARING
   ↓
PROGRAMMING_FPGA
   ↓
PROGRAMMING_SOC
   ↓
RUNNING
   ↓
SUCCEEDED
```

or:

```text
FAILED
TIMED_OUT
CANCELED
INTERRUPTED
```

The system must also satisfy:

```text
Mac queue survives daemon restart

queued jobs survive Mac reboot

TTL state survives Mac reboot

FPGA SHA cache does not survive Mac reboot

scheduler restart does not kill running jobs

short scheduler/network outage does not kill running jobs

scheduler can reconcile state after reconnect

users can complete the entire normal workflow from fl-client

physical firmware commands are isolated behind BoardBackend
```

---

# 43. Intended Final System

```text
                         USER ENVIRONMENT

                  arbitrary Chipyard host
                           │
                      fl-client
                           │
                    HTTPS / BBCP
                           │
                           ▼

                  CONTROL / DATA PLANE

               ┌───────────────────────┐
               │ Scheduler Server      │
               │                       │
               │ FastAPI               │
               │ React UI              │
               │ PostgreSQL            │
               │ OAuth                 │
               │ Google Groups         │
               │ WebSocket Gateway     │
               │ Headscale             │
               └──────────┬────────────┘
                          │
                  private Headscale
                          │
              ┌───────────┼────────────┐
              │           │            │
              ▼           ▼            ▼
          Mac Mini     Mac Mini     Mac Mini
              │
          fl-agent
              │
      ┌───────┼────────┐
      │       │        │
      ▼       ▼        ▼
    Board0  Board1   Board2
      │       │        │
   FPGA/SoC FPGA/SoC FPGA/SoC


       Transfer Gateway
              │
              ├── public-facing BBCP
              └── Headscale-facing BBCP


       License Relay
              │
              ├── Headscale
              └── BWRC license network


       Optional future:
       Kasa Force-Off
              │
              X
           Mac power
```

The governing design principle is:

```text
scheduler decides where work goes

Mac agent decides what actually happens to hardware

fl-client handles the complete remote user workflow

Headscale protects internal infrastructure

transfer gateway handles large files

Google Groups determines who is allowed to use the system

firmware integration is isolated behind a replaceable backend
```

This lets the scheduler, networking, queueing, CLI, authentication, transfer, and monitoring stack be implemented and tested now, while the actual PCB firmware remains incomplete and can be integrated later without redesigning the rest of the platform.