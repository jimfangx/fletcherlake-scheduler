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
files are replaced; use the agent upgrade/reconfigure workflow for an existing installation.
If enrollment succeeded but confirmation was interrupted, run
`sudo /opt/fl/.pixi/envs/default/bin/fl cluster setup confirm /opt/fl` on the Mac. Inspect the
individual board states after deployment; the blank SophiaLake backend stays in `ERROR`
until its hardware functions are implemented. See [board backends](../docs/board-backends.md).

Scripts `19` and `20` are the original manual Mac steps; they are not additional steps after
a successful `18` deployment. The numbered `00` driver still references the old filenames
from before the rename and must be updated before use. Run `18` directly as shown above.
