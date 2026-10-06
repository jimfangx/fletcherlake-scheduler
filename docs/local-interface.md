# Local terminal interfaces

The production agent and setup tools target macOS. The dashboard uses the local daemon's
permission-restricted Unix socket; it never queries the scheduler, a firmware command, or a
board backend. `FL_AGENT_SOCKET` selects another local socket for development.

```sh
pixi run fl cluster status
pixi run fl cluster status --dashboard
```

Plain status retains its JSON output for scripts. The Textual dashboard has five tabs:

| Tab | Contents |
| --- | --- |
| Boards | Physical ID, configured SoCs/FPGAs, daemon state, active job, queue count and board error |
| Jobs | Owner, full UUID, board, elapsed execution time, state and priority |
| Queues | Board, position, job, owner and priority in the daemon's reported order |
| Artifacts | Owner, job, file kind, byte size and time until deletion |
| System | CPU/RAM percentages, used/total disk space and the daemon's scheduler connection status |

Use Tab and arrow keys to navigate, Enter on a job row for details, Escape to close details,
`r` to refresh, and `q` to quit. Wide tables scroll horizontally. Sizes use binary units
(KiB/MiB/GiB), and timestamps include the local timezone. A completed job's elapsed time is
frozen at its finish timestamp; queued jobs show no execution time.

Every view uses one validated daemon snapshot. Refresh runs in a worker so a socket request
does not freeze navigation. Only one request can be in flight; a slow read causes refresh
ticks to be skipped rather than overlapping reads. The normal interval is two seconds.
Connection or malformed-response failures preserve the last tables and show an unavailable
banner with the last snapshot timestamp. A successful subsequent read restores the banner.
Elapsed time and deletion countdowns use the snapshot's timestamp, so a failed refresh cannot
make old data appear current. `Due` indicates expiry, while `Deleted` requires an actual daemon
deletion record. An unset expiry reads `After completion`.

## Interactive configuration

```sh
pixi run fl cluster setup init --scheduler https://scheduler.example.edu --interactive
pixi run fl cluster setup init /path/to/overrides.yaml \
  --scheduler https://scheduler.example.edu --interactive
pixi run fl cluster setup reconfigure /opt/fl --interactive
pixi run fl cluster setup reconfigure /opt/fl --config /path/to/overrides.yaml --interactive
```

Setup still requires macOS and administrator privilege. Init seeds the form by merging
explicit YAML overrides over host/tool detection; interactive edits then become deliberate
operator overrides. An optional input file must explicitly supply one to three board slots.
Without a file, interactive init starts with three disconnected slots. A configuration with
`boards: [null, null, null]` does the same. Detection
never fabricates boards, and adding a board requires an explicit identifier and backend.
Existing inputs and caller models are not mutated by the editor.

The editor has Host, Boards, and Tools & power tabs. Select a board slot and press Enter, or
use Add / edit selected. Board forms include ID, `mock`/`lilikoi` backend, clock source,
rail/sensor counts and firmware command timeout. The Devices & firmware tab accepts standard
YAML for FPGA and SoC lists, device mappings, and operation-to-argv firmware mappings. Nested
host OS/tool information and optional power-control configuration also use standard YAML.
Field names and firmware placeholders follow [the adapter contract](macos-operations.md#firmware-adapter-contract).
Selecting the Kasa scaffold records configuration only; it has no plug action.

Save board (or Ctrl+S inside its modal) validates that board and updates the in-memory draft.
Cancel or Escape discards only that board's draft. Clear selected makes the slot disconnected.
Save configuration/Ctrl+S in the main editor validates the entire `ClusterConfig`, including
unique board IDs. Errors leave the form open with field paths and a concise message. Cancel
or Escape from the main editor returns no configuration; the editor never writes files itself.

Interactive reconfigure without `--config` edits the existing inventory. The shared lifecycle
workflow removes/fsyncs confirmation and drains the daemon before opening the editor. Saving
then preserves the enrolled UUID, writes the validated inventory, synchronizes Pixi, confirms,
and reloads launchd. Canceling keeps confirmation absent and the daemon drained; correct and
confirm the configuration to restore readiness. `--freeform` uses `$EDITOR` instead and cannot
be combined with `--interactive` or `--config`.

## Implementation and verification

Dashboard rendering, formatting, metrics and detail screens live in separate short modules
under `fl_cli/dashboard`. Editor fields, board forms and inventory forms are separated under
`fl_cli/inventory_editor`; the library lifecycle has no terminal UI dependency.

Headless Textual checks exercise keyboard and mouse interaction, compact 80×24 editing,
validation, explicit backend selection, cancellation, identity preservation, and firmware argv
paths containing spaces. Dashboard checks include actual local agent HTTP reads while a mock
board is running, literal text rendering, snapshot timing, selection preservation, modal
refresh, failed reads/recovery and one in-flight socket request. Linux checks use explicit
simulation and injected native effects. Real Mac terminal, launchd and hardware acceptance
still require macOS; the scheduler React interface is a separate remaining deliverable.
