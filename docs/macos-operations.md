# macOS operations

Deploy this checkout in a stable system location, for example `/opt/fl`, and install Pixi.
Board definitions must come from the operator. Use `examples/cluster.yaml` only for simulation;
real boards select `backend: lilikoi` and provide firmware command mappings.

The current Mac tool setup uses Vivado Lab and does not require BWRC license forwarding.
Leave `license_relay` omitted or `null` in the Linux deployment inventory. Tools that later
need a network license can opt into the [license relay](license-relay.md) independently.

Install rclone and OpenSSH before accepting remote collateral. See the
[checksum-pinned installation](transfer-gateway.md#installation-and-acceptance) for Linux and
native Apple Silicon/Intel Mac binaries. Put the binary in a stable location and set
`environment.rclone.path` in the cluster overrides if it is outside the daemon's PATH.
Detection records the installed executable when available; launchd includes `/opt/homebrew/bin`
and `/usr/local/bin` in PATH. Native Mac transfer acceptance remains a deployment gate;
Linux tests use an explicitly supplied rclone binary.

```sh
pixi run fl cluster setup init /path/to/cluster-overrides.yaml --scheduler https://scheduler.example.edu
pixi run fl cluster setup confirm /opt/fl
pixi run fl cluster status
```

System lifecycle commands acquire sudo and re-execute before writing system-wide files.
Autodetection fills tools, macOS release/build, model, and MAC address where available. Explicit
YAML values override detection. `--interactive` opens the configuration editor for host,
tools, optional power configuration and explicit board definitions.
No detected hardware inventory is invented. Configuration is initially unconfirmed.
Install Tailscale and configure scheduler/Headscale deployment first. An administrator issues
a short-lived enrollment ticket; enter its token at init's hidden prompt. Setup registers a
stable UUID, writes protected credentials separately from inventory, and joins with a temporary
key file. See [private-network operations](headscale-operations.md) for issuance and recovery.

Confirmation runs a locked Pixi installation, restricts the state/runtime directory modes,
installs `edu.berkeley.fletcherlake.agent.plist` in `/Library/LaunchDaemons`, writes the config
SHA, and bootstraps/kickstarts the daemon. State is in `/Library/Application Support/fl`;
runtime cache, locks, and socket are in `/var/run/fl`. Configuration and confirmation marker
are mode 0600. The service logs are in the state's `logs/` directory. Confirmation waits for
the local API to report READY, retrying asynchronous startup for up to 90 seconds. An invalid
confirmation marker fails promptly.

```sh
pixi run fl cluster setup reconfigure /opt/fl --config /path/to/new-config.yaml
pixi run fl cluster setup reconfigure /opt/fl --interactive
pixi run fl cluster setup reconfigure /opt/fl --freeform
pixi run fl cluster restart
pixi run fl cluster destroy
```

Reconfiguration removes and fsyncs the marker immediately, drains hardware, validates the edited file,
synchronizes dependencies, confirms, and restarts launchd. Restart first drains the daemon,
interrupts active jobs, powers down boards, records lifecycle state, and checkpoints SQLite;
only then does the CLI request `/sbin/shutdown -r now`. A board-shutdown error blocks that
request. Restart preserves queued work. Reconfiguration preserves the registered UUID; a
replacement inventory can omit it, but cannot replace it. Destroy interrupts active jobs,
cancels queued work, finalizes retention, and sends the final snapshot with authenticated
unregister before stopping launchd, logging out Tailscale, and removing local credentials and
definition. A failed unregister leaves credentials available for retry. The scheduler fences
the old session and retries node revocation durably. A protected journal makes native teardown
retryable. History moves to a cluster-scoped archive, and a separate launchd job continues
expiry cleanup after the agent is removed. A later enrollment starts a fresh database under
a new UUID. See [permanent retirement](retirement.md) for recovery, paths and retained data.
See [local terminal interfaces](local-interface.md) for dashboard navigation, editor fields,
validation and cancellation behavior.

If reconfiguration is killed during editing or dependency installation, its confirmation marker
stays absent. A restarted daemon reports `CONFIGURATION_INCOMPLETE`, retains queued jobs, and
rejects new execution. Correct the configuration and rerun `fl cluster setup confirm /opt/fl`.
Confirmation queries launchd registration before removing an installed service, so it also
recovers when the previous management process died after `bootout` and before `bootstrap`.
The installed plist alone does not establish that the service is still loaded. Unknown native
query failures propagate rather than being treated as an absent service.

Linux crash acceptance kills the real management process at editor, Pixi-sync and completed
service-removal boundaries while a real local agent has active and queued mock work. It checks
UUID/queue preservation, stable completed-job retention, input hashes, execution refusal before
confirmation, and one subsequent execution of the queued job. Privilege, dependency installation
and launchd effects are injected; this does not establish native `launchctl` behavior on a Mac.

## Python management

`fl.ClusterSetup` implements init, confirm, reconfigure, restart, and destroy. The CLI calls
these same workflows; hardware shutdown still goes through the daemon's Unix socket API.
Run management code on the Mac Mini with administrator privilege. Library methods check macOS
and privilege before side effects; they never acquire sudo, re-execute the caller's script,
or prompt. The CLI retains its sudo and terminal prompts.

```python
from pathlib import Path
from fl import Cluster, ClusterSetup

setup = ClusterSetup()
# Obtain a short-lived token through your application's protected input mechanism.
# setup.init(Path("/path/to/cluster-overrides.yaml"),
#            scheduler="https://scheduler.example.edu", enrollment_token=token)
ready = setup.confirm(Path("/opt/fl"))
with Cluster.local() as cluster:
    print(cluster.status())
```

Init accepts a YAML `Path`, a mapping with explicit boards, or a validated `ClusterConfig`.
Mappings and YAML merge explicit overrides over macOS detection. A validated model is copied
without further detection, preserving explicit nulls and the caller's model. Init creates an
unconfirmed definition and uses the same protected retry receipt after a lost registration
response; retry with the original inventory and token.

`setup.reconfigure(Path("/opt/fl"), Path("/path/to/new-config.yaml"))` removes the confirmation
before draining and parsing the replacement. Omitting the replacement validates the existing
file. An optional `editor(path)` callback runs after drain; a `config_factory()` callback can
collect interactive values at that point. Supply either a replacement or a factory. Failure
keeps the marker absent; correcting the configuration and confirming restores readiness.
Confirm and reconfigure wait up to 90 seconds by default and accept `timeout=`.

`setup.restart()` waits for daemon shutdown acknowledgement before requesting a Mac reboot.
`setup.destroy()` sends the final snapshot before native teardown and returns that snapshot.
Unregister failure preserves local recovery data. `scheduler=` records a public unregister
origin for legacy credentials. The same archive, independent cleanup, and retry behavior
described in [permanent retirement](retirement.md) applies to Python callers.

## Firmware adapter contract

`firmware_commands` is a mapping from backend operation name to argv arrays. Values in braces
are substituted within argv items; there is no shell expansion. Paths with spaces remain one
argument. Available fields are `{bitstream}`, `{elf}`, `{rail}`, `{volts}`, `{channel}`, and
`{hz}` for their respective operations. Keys from `device_mapping` also supply named device
placeholders, such as `{uart}`. Literal JSON objects remain literal argument text.

Implement operations from `fl_agent.hardware.base.BoardBackend`. `discover` emits a JSON
string mapping; sensor reads emit a numeric value; `read_uart` emits bytes; `wait_for_completion`
emits a JSON `RunResult` (at least `passed` and optionally `exit_code` and `detail`). Other
operations report success by exit status. `power_on` must preserve already powered FPGA state;
`power_off` resets the physical platform. `stop` must safely stop execution and be idempotent.

Each firmware command has a deadline, configured with `firmware_timeout_seconds` on its board
(default 60 seconds); the job's own deadline covers preparation and execution.
A firmware completion command that requires a long wait needs an appropriately configured
adapter command deadline. The supplied firmware syntax is still unknown, so production command
mappings must be supplied and tested by the hardware team.

## Recovery

Do not remove `agent.db` to fix a stuck queue. Inspect job events and board errors, correct the
hardware/firmware cause, and restart the service to perform safe board recovery. If configuration
is incomplete, validate and confirm it. Do not create an arbitrary SHA to bypass validation.
Physical restart, launchd behavior, and actual board shutdown still require validation on macOS.
