# Mac deployment

`18-bringup-mac.sh` performs a **destructive fresh installation** of the Mac agent that hosts boards.
It stops the old agent and its helper processes, then deletes `/opt/fl`, `/opt/fl-releases`,
`/opt/fl-tools`, `/Library/Application Support/fl`, `/Library/Application Support/fl-retired`
and `/var/run/fl`. Existing credentials, enrollment receipts, configuration, job history,
queued work, retained artifacts and release backups in those directories are permanently removed.
It requires a fresh enrollment ticket and creates a new cluster UUID. The installation also
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
the Mac and is never passed in SSH arguments. After collecting the ticket and resolved inventory,
setup hands enrollment and confirmation to `edu.berkeley.fletcherlake.enrollment` under launchd.
Its protected request and progress files live under `/Library/Application Support/fl`, and its
logs are `logs/enrollment.stdout.log` and `logs/enrollment.stderr.log` there. The foreground
script reports that work is queued, rather than claiming the agent is already enrolled.
The job continues after SSH disconnects: it joins Headscale, registers the cluster, confirms
the installation and waits for the agent's scheduler connection. It retries transient failures
for up to ten minutes and records `COMPLETE` or `FAILED` in `enrollment-progress.json`.
Successful enrollment removes the staged ticket request. The cluster is then visible at
`https://scheduler.<domain>/clusters`; individual hardware errors are reported separately.

Enrollment explicitly resets Tailscale settings and reauthenticates with the issued key,
which can disconnect Tailscale SSH. A retry recognizes a successfully joined node for the same ticket
and resumes registration without reusing its one-time join key. Tailscale failures include
the command's diagnostic with authentication keys redacted.

On the target Mac, inspect progress with
`sudo cat '/Library/Application Support/fl/enrollment-progress.json'`. To resume a queued or
failed attempt with its saved ticket/configuration, run
`sudo /opt/fl/.pixi/envs/default/bin/python /opt/fl/ship/enroll-mac-background.py --resume`.
An expired/revoked ticket needs a new destructive installation and a fresh ticket.

Install and update scripts provide `fl` and `fletcherlake` in `/usr/local/bin`, pointing to
the active CLI under `/opt/fl`. They add a managed PATH block to the SSH user's `.zshrc`
(or `$ZDOTDIR/.zshrc`) and preserve a backup before its first edit. Open a new terminal or
run `source ~/.zshrc` to use the commands. Existing unrelated commands with either name
are reported rather than overwritten. Running `fl` or `fl --help` shows the full command
tree, including required arguments. For example, confirmation is `sudo fl cluster setup confirm /opt/fl`.

This reset runs on every invocation, including on an already enrolled/running Mac, without
an extra confirmation prompt. The operator's `~/cluster-overrides.yaml`, shell settings,
Homebrew and Tailscale installation are retained. The reset removes local state; old
server-side cluster records remain in the scheduler. A previously registered cluster must
be retired separately before reusing its board IDs/network node for a fresh enrollment.
Use `19-2-update-existing-mac.sh`
when updating an existing installation while preserving its enrollment, configuration and jobs.
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
