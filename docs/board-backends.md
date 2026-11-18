# Implementing a board backend

Every physical board has a `board_id` and a `backend` in the Mac's cluster configuration.
The agent constructs one `BoardBackend` per board and runs an independent queue for it.
Different backend types can share a Mac (up to three board slots) or run on separate Macs.
The scheduler, artifact transfer and job executor use the same hardware interface.

## SophiaLake template

Implement the methods in
[`SophiaLakeBoardBackend`](../packages/fl-agent/fl_agent/hardware/sophialake.py), preserving
the signatures in [`BoardBackend`](../packages/fl-agent/fl_agent/hardware/base.py).
The template is selectable as `backend: sophialake` in YAML and the interactive setup editor.
Every hardware method currently raises `BACKEND_NOT_IMPLEMENTED`; it provides no hardware
support until the board creator fills it in. The constructor only records configuration.

Start with [`examples/cluster-sophialake.yaml`](../examples/cluster-sophialake.yaml).
Supply real rail/sensor counts, clock source and SoC/FPGA labels. Define and validate the
backend's `device_mapping` keys when implementing it. These can contain stable USB identities,
tool paths and paths to trusted board/chip configuration files installed on the Mac. No
SophiaLake mapping keys or electrical limits are assumed by this template.

For example, if the implementation needs separate FPGA and chip OpenOCD configurations,
define separate keys and consume them in `program_fpga` and `program_soc`, respectively.
Adding a key alone does not execute it. Baremetal-IDE linker scripts, headers and platform
sources stay in the build project that creates the ELF.

The implementation must observe these semantics:

| Method | Required behavior |
| --- | --- |
| `discover()` | Probe identity/health; return `dict[str, str]`. The agent does not continuously call this or automatically generate inventory. |
| `power_on()` | Apply the board's power sequencing and electrical limits. |
| `power_off()` | Idempotently power off, including when already off or disconnected; report inability to establish the required state. |
| `set_voltage(rail, volts)` | Use a zero-based rail index and volts; validate limits before changing hardware. |
| `read_voltage(channel)` | Return volts from a zero-based sensor channel. |
| `read_current(channel)` | Return amperes from a zero-based sensor channel. |
| `set_frequency(hz)` | Set hertz within validated operating limits. |
| `program_fpga(bitstream)` | Program the provided local file and confirm success. The executor may skip an unchanged bitstream using its per-board cache. |
| `program_soc(elf)` | Load or stage the local ELF; execution begins through `start()`. |
| `start()` | Start execution and establish console/completion tracking. Shmoo trials can call this repeatedly after `stop()`. |
| `stop()` | Idempotently halt/reset execution and clean up owned processes. |
| `read_uart()` | Return new console bytes without repeating them; return `b""` when nothing is available. It runs concurrently with completion detection. |
| `wait_for_completion()` | Wait for real completion and return `RunResult(passed=..., exit_code=..., detail=...)`. A successful upload or start does not establish a successful run. |

Startup calls `stop()` and `power_off()` before marking a board ready. Until these methods
are implemented, the SophiaLake board remains in `ERROR`, and its queue does not execute.
The cluster can still report `READY`; inspect each board's state. Other working boards retain
their independent workers. Cleanup failures also put that board in `ERROR` until operator
recovery and an agent restart.

Use asynchronous I/O or move blocking calls off the event loop. Bound hardware operations
using `self.timeout` (from `firmware_timeout_seconds`); the template stores it but does not
automatically wrap implementations. The worker separately enforces the job's `run_timeout`
and ten-second stop/power-off cleanup deadlines. On cancellation, terminate/reap owned
processes and release hardware resources. Raise `PlatformError` with useful operation details
on failures; unsupported features should raise an explicit `UNSUPPORTED` error. Implement
optional operations when that hardware supports them and report only real sensors/rails.

## Selecting a board for a job

An administrator chooses the implementation in the cluster override:

```yaml
boards:
  - board_id: sophialake-0
    backend: sophialake
    device_mapping: {}  # Fill with keys consumed by the completed implementation.
  - null
  - null
```

Users choose the physical board in their **job YAML**:

```yaml
resource_constraints:
  board: sophialake-0
binary: ./build/app.elf
# bitstream: ./build/top.bit  # Include when this board needs FPGA programming.
run_timeout: 3600
run_collateral_ttl: 30
```

Use [`examples/job-sophialake.yaml`](../examples/job-sophialake.yaml) as a starting point.
Submit it with `pixi run fl-client submit /path/to/job.yaml --follow`. Paths to inputs are
resolved relative to that YAML file. `board` must match the configured board ID exactly;
board IDs must be unique across the fleet. The job does not select or override `backend`.

To allow any compatible board, omit `board` and request `soc` and/or `fpga` under
`resource_constraints`. These match `socs[].name` and `fpgas[].model` exactly. All supplied
constraints must match the same board. Labels are inventory identifiers; they do not load
configurations or validate an ELF/bitstream's compatibility. If all constraints are omitted,
any available eligible board can be selected.

## Installing the implementation

Deploy the updated software to the scheduler and Mac agent before enrolling/reconfiguring a
SophiaLake board: older software rejects the new backend name. Any other software validating
cluster inventories needs the same schema update. No database schema or dependency changes
are required. Install any hardware tools/configuration files needed by the completed backend
on the Mac, then apply the board override through the existing setup/reconfigure workflow.

Validate programming, execution/completion, console capture, cancellation and recovery on
actual SophiaLake hardware before accepting users' jobs. The blank template has no board
electrical assumptions and has not been validated on physical hardware.
