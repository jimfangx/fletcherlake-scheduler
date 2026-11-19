# Mac deployment

`18-bringup-mac.sh` installs a **new Mac agent** that hosts boards. The installation also
provides the local dashboard and the `fl-client` submission CLI. Run it on your operator
computer from a terminal:

```bash
FL_DOMAIN=fl.ucb.bar bash ship/18-bringup-mac.sh my-mac
```

`my-mac` can be a `Host` alias in `~/.ssh/config`, including configured user, port, identity
and jump hosts. The script uses normal `ssh` and `scp` without overriding those settings.
Alternatively set `FL_MAC_SSH` in the environment. On the Mac, a short terminal wizard
creates `~/cluster-overrides.yaml`. It asks for the number/type of boards and, for Fletcher
Lake, the installed chip name. It detects RP USB MCU serials and FPGA FTDI channel-A ports;
when several are attached, select the correct MCU/FPGA pair for each board. Missing device
identifiers can be entered manually. The separate SoC-debug FTDI is excluded from UART-TSI
auto-selection. JTAG serials are taken from the selected FPGA's USB descriptor when present.

Board IDs are generated from the Mac hostname, MAC identifier and board slot, then printed
for use in job YAML. Host details, tool locations, sensor counts and FPGA model do not need
answers. The Fletcher Lake profile uses `xc7k160t` and the bundled FPGA OpenOCD configuration.
Optional chip-owner voltage limits are the only electrical setting requested: leave blank
to disable voltage changes. Existing MCU regulator routing is preserved. The wizard only
enumerates USB ports; it does not power up or probe the attached chips.

An existing configuration can be reused with one prompt. For advanced/custom hardware
settings, a prepared file is still supported as an optional second argument or `FL_MAC_CONFIG`.
Generated files have mode 0600 and are written after all answers validate. The SophiaLake
choice produces the blank template configuration; its hardware implementation is still required.

The script checks scheduler HTTPS health, builds a fresh `$FL_STATE/mac-source.tar` (default
state directory `~/.local/state/fl-deploy`), uploads it and runs installation over an SSH
terminal. It includes current tracked files and nonignored new files, including uncommitted
changes and lilikoi's host code. Git metadata, ignored build/environment files, the deployment
state directory and the firmware-only Pico SDK are excluded. It initializes lilikoi if
missing, without recursively downloading firmware SDKs. It does not use `FL_REPO_REV` or
replace the cloud deployment's `source.tar`.

The Mac must already have macOS 13+, Apple's Command Line Tools, approved/running native
Tailscale, Remote Login and an administrator account with sudo access. Homebrew must already
be installed for `lilikoi` boards. The script installs pinned Pixi (default v0.81.0), the locked
environment and verified rclone; for `lilikoi` it installs OpenOCD/Spike through Homebrew's
owning account and builds native `uart_tsi`. Firmware flashing remains a board preparation step.

Installation prompts for sudo, then for a fresh enrollment ticket from
`https://scheduler.<domain>/admin/clusters`. The ticket is entered at a hidden prompt on
the Mac and is never passed in SSH arguments. Setup enrolls the agent, installs launchd and
prints status. Use LAN SSH or a connection that will survive joining the deployment's
Headscale network: enrollment may disconnect SSH using a different existing Tailscale tailnet.

This is initial installation. An already enrolled/running Mac is rejected before application
files are replaced; use `19-2-update-existing-mac.sh` for an existing installation.
If enrollment succeeded but confirmation was interrupted, run
`sudo /opt/fl/.pixi/envs/default/bin/fl cluster setup confirm /opt/fl` on the Mac. Inspect the
individual board states after deployment; the blank SophiaLake backend stays in `ERROR`
until its hardware functions are implemented. See [board backends](../docs/board-backends.md).

## Update an existing Mac

Run from a current source checkout on your operator computer:

```bash
bash ship/19-2-update-existing-mac.sh my-mac
```

Use the same SSH hostname or config alias as bring-up, or set `FL_MAC_SSH`. No domain,
enrollment ticket or new board inventory is needed. The Mac must already have a running,
enrolled agent at `/opt/fl`, its installed Pixi and hardware tools, and sudo access.
This command also works from a source checkout on the Mac using `localhost` with SSH enabled.

The script packages current tracked and nonignored new files, including uncommitted changes
and lilikoi host code, as `$FL_STATE/mac-update-source.tar`. Git metadata, ignored files,
deployment state and the firmware-only Pico SDK are excluded. It uploads via SSH/SCP with
your existing SSH config, prepares a locked environment in `/opt/fl-releases/mac-update.*`,
and rebuilds native `uart_tsi` when the current inventory uses lilikoi. Preparation failures
leave the running installation in place.

Activation uses the lifecycle management lock and reconfiguration workflow. It invalidates
confirmation, drains hardware, interrupts active jobs, stops launchd, switches `/opt/fl` to
the prepared release, confirms the existing inventory and waits up to 90 seconds for READY.
Queued work, the registered UUID, credentials, database and retained job data stay in place.
There is no enrollment or Mac reboot. Board-shutdown failures block the source switch.

The first update retains the original `/opt/fl` directory as
`/opt/fl-releases/legacy-mac-update.*`; subsequent updates retain the previous release.
The new release's protected `previous-installation/` directory saves the old config, launchd
definition and `previous-project` path. Releases are retained for manual recovery; automatic
rollback is not attempted because new code may have migrated the database. Inspect and
correct any error before running the printed `setup confirm /opt/fl` recovery command.
Do not delete a release referenced by `/opt/fl` or a launchd definition. For inventory edits,
use `fl cluster setup reconfigure /opt/fl --config /path/to/new-config.yaml` separately.

For a new Mac, run `18-bringup-mac.sh` directly as shown above. The old manual setup scripts
have been removed. The numbered `00` driver is an older deployment guide and is not a supported
way to install a Mac agent.
