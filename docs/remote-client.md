# Remote terminal client

`fl-client` runs as an ordinary Linux/macOS user and contacts the public scheduler through
HTTPS. It does not enroll the user machine in Headscale or access a Mac directly.

Install the repository's locked environment, then log in:

```sh
pixi run fl-client login --scheduler https://scheduler.example.edu
```

The command prints an HTTPS verification URL and a terminal code. Open the URL in any browser,
sign in with Google, then enter the code shown on your own terminal. The browser can be on
another computer: there is no localhost callback or desktop browser requirement on an SSH
host. Group membership is required before approval can grant the terminal a session.

Implemented commands:

```sh
pixi run fl-client submit examples/job.yaml
pixi run fl-client jobs
pixi run fl-client status JOB_UUID
pixi run fl-client status JOB_UUID --follow
pixi run fl-client logs JOB_UUID --follow
pixi run fl-client cancel JOB_UUID
pixi run fl-client results JOB_UUID
pixi run fl-client logout
```

These return the scheduler's replicated metadata. Cancellation is asynchronous; use status to
observe the agent's outcome. The default credential file is
`$XDG_CONFIG_HOME/fletcherlake/client.json`, or `~/.config/fletcherlake/client.json` when XDG is
unset. Each command accepts `--credentials PATH` for an alternate profile. Its parent directory
must belong to the current user and have mode 0700. Files and the rotation lock have mode 0600;
symlinks and public permissions are rejected. Tokens are masked in model representations and
never printed by the CLI. Writes use atomic replacement and fsync.

Access tokens refresh automatically under an OS file lock shared by CLI processes. A rejected
access token triggers one refresh and one retry; refresh failures never loop indefinitely.
Client HTTPS verification stays enabled, and API redirects are rejected to prevent credential
forwarding. The stored profile determines the scheduler origin for subsequent commands.
Logout revokes the remote session before removing local credentials; if the server is
unreachable, the command fails and preserves them for a later retry. If credentials cannot be
saved after a successful one-use refresh, log in again.

## Submit and resume

Install BBCP and OpenSSH on the user host. The command resolves YAML input paths relative to
the YAML file, hashes each input, and saves a protected receipt before sending job metadata.
The receipt contains the immutable request ID, authenticated owner, per-job SSH identity binding,
and staging token.
Keep this directory private. By default it is created under the credential directory at
`submissions/<random-UUID>/receipt.json`; its location is printed before submission.
Use `--receipt PATH` to choose a different protected location.

```sh
pixi run fl-client submit examples/job.yaml --priority -2 --timeout 3600 --ttl 30
pixi run fl-client submit examples/job.yaml --follow
pixi run fl-client submit --resume /path/to/receipt.json
```

Submission automatically starts terminal approval when a cached session is missing or expired.
For an initial submission without a profile, supply `--scheduler https://scheduler.example.edu`.
An expired profile supplies its existing scheduler origin. `--scheduler` must match an existing
profile; use `--credentials` with a separate file for another scheduler. Provider/directory
outages are reported without starting repeated approval attempts.

The scheduler assigns the trusted owner and job UUID. The client waits for a board reservation
before obtaining an upload grant. It transfers bytes directly to the public gateway through
BBCP with a pinned SSH host key, verifies the complete manifest through the gateway's HTTPS
API, then requests delivery from the scheduler. The Mac initiates the private download and
verifies bytes before execution. The terminal command returns after delivery is accepted;
use `status` to monitor execution. `--follow` displays durable state transitions through the
authoritative terminal outcome. Jobs without inputs are automatically enqueued after reservation.
User hosts require neither root nor Headscale enrollment.

If interrupted or a response is lost, run `--resume` with the printed receipt. Identical requests
return the same job UUID; accepted delivery is reused. Resume requires the original user's login,
including when the first submission response was lost. A verified gateway upload is reused after
checking its manifest. Resume preserves all original options and rejects changed local inputs
before an upload. To intentionally create another job, submit with a fresh receipt.
The receipt uses mode 0600, its directory uses 0700, and the identity is stored beside it as
`<receipt-name>.key`. Receipt and identity must stay together. Deleting them loses retry proof;
monitor or cancel the known UUID instead of guessing whether a new submission duplicates work.

The scheduler stores only the staging-token hash and public identity. Human session credentials
go only to the scheduler; verification sends the separate staging token to the gateway. Both
HTTPS paths reject redirects. BBCP's public payload sockets are unencrypted; see
[the transport boundary](transfer-gateway.md#protocol-and-scope).

Python callers can use `Submission(RemoteClient(...)).run(ReceiptStore(path), config)` and
resume with `.run(ReceiptStore(path))`.

## Retrieve results

```sh
pixi run fl-client results JOB_UUID --output /path/to/job-results
pixi run fl-client results JOB_UUID --output /path/to/job-results --inputs
```

Results require a terminal job and retained agent artifacts. By default the command downloads
available `job.json`, `results.json`, `stdout.log`, and `stderr.log`. `--inputs` also includes
the original `binary` and `bitstream`. Optional artifacts, such as stderr on a successful job,
are omitted when absent. Only the job owner or an authorized operator/admin can request a read.
The default directory is `results-JOB_UUID` under the current working directory.

The scheduler requests an exact immutable manifest from the assigned Mac. The Mac creates a
protected export identity and uploads through the private BBCP listener. The scheduler hashes
and seals those bytes on the gateway before issuing a public read grant bound to the user's
separate SSH key. The user initiates the public download and independently checks SHA and size.
These artifact downloads use BBCP. User hosts need no root or Headscale membership;
Macs need no incoming SSH listener. Credentials expire within ten minutes and cannot exceed
artifact retention. Exported copies retain the same completion-based expiry as their agent source.

Repeating the command with the same output directory resumes the saved request and original
artifact selection. Its private `.download/receipt.json` and identity are written before the
request and bind the scheduler, job, user and manifest. Keep this directory with the output files.
Verified local files are reused. Different existing files are never overwritten, including a
concurrent writer's file. Downloads become visible only after independent verification and fsync.
Use a fresh directory to change the selected artifacts or retry a permanently failed export.

Artifact deletion blocks new scopes immediately. The export worker revokes gateway source/read
scopes, and the agent cancels/reaps publication before deleting files. Gateway expiry and revocation
are rechecked during streaming. Existing downloaded files remain the user's responsibility.
Python callers can use `Results(client).run(job_id, destination, inputs=True)`.

## Live logs and state following

```sh
pixi run fl-client logs JOB_UUID
pixi run fl-client logs JOB_UUID --follow > stdout.log
pixi run fl-client logs JOB_UUID --stream stderr --offset 4096 --follow > stderr-tail.log
pixi run fl-client status JOB_UUID --follow
pixi run fl-client submit --resume /path/to/receipt.json --follow
```

Log output preserves raw bytes, including non-UTF-8 UART output. Authentication prompts and
errors go to stderr. Without `--follow`, the command reads up to the first advertised watermark;
with it, the command waits for new bytes and stops at terminal EOF. `--offset` resumes from a
byte position, independent of text lines. Missing optional terminal stderr is an empty stream.
Only retained logs are readable; deletion or expiry denies cached bytes immediately.

Temporary scheduler/transport failures preserve byte offsets and state-event cursors. Each retry
uses normal authentication and refresh; revoked authorization stops monitoring. Cached ranges
may be available while the Mac is disconnected; uncached ranges wait for reconnection. Press
Ctrl-C to stop waiting. Monitoring does not cancel or restart the hardware job.

Python callers can iterate `Monitor(client).logs(job_id, follow=True)` for verified `bytes`, or
`Monitor(client).states(job_id)` for state names. See [the protocol and operating details](live-logs.md).
