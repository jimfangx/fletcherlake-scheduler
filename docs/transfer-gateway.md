# BBCP gateway

The Linux gateway receives large collateral separately from scheduler metadata. The shared
`fl_common.bbcp.BBCP` client runs on a user's computer or a Mac agent. It pins the gateway's
Ed25519 SSH host key, uses a separate protected identity for each transfer, and terminates the
whole process group at the credential deadline. Mac agents initiate downloads; the gateway
listens for their BBCP connections through Headscale. Macs need no inbound SSH server.

The transport, gateway APIs, public reserved upload grants, protected terminal receipts,
scheduler delivery worker, and Mac fetch commands are implemented. `fl-client submit` automates
both transfer hops and resumes the same job after lost responses. `fl-client results` exports
terminal artifacts from the Mac and downloads independently verified files through public read
scopes. See [the terminal workflows](remote-client.md).

## Protocol and scope

BBCP starts the remote `bbcp SNK` or `bbcp SRC` command over SSH, then sends options and a path
on stdin. Its control and payload connections use independent TCP sockets. These details are
verified against [the pinned official source](https://github.com/slaclab/bbcp/tree/866ba250b8e7915f23e159a1060038933e829780).
The public BBCP payload sockets are unencrypted; SSH protects bootstrap and Headscale encrypts
the private hop. SHA-256 checks provide content integrity, not public-hop confidentiality.

A forced SSH command by itself cannot confine BBCP's filesystem operations. The guard rejects
arbitrary commands, options, recursion, config files, programs, paths, and callbacks, then
replaces BBCP's arguments with a fixed program pipe. Only the gateway's stream helper reads
or writes bytes. The client supplies a random 256-bit data-session token through a mode-0600
BBCP configuration file; the token is absent from process arguments and job metadata.

Each forced command reserves one available listener port within the configured gateway range.
OS-held per-port locks coordinate independent SSH processes; the guard passes that single port
to BBCP and retains the lock until its native process group is terminated and its leader reaped.
Waiting consumes the existing credential deadline. Occupied ports are skipped, and process death
releases the lock even if its file remains. The socket probe closes before BBCP binds, so an
unrelated process can still race that bind; dedicate the range to this gateway.

Each `TransferGrant` fixes the job UUID, transfer UUID, artifact kinds, SHA-256, byte sizes,
public identity, staging-token hash, credential expiry, and payload retention deadline.
Credentials are bounded to ten minutes. Retention is separate and bounded to 365 days.
Downloads refer to a verified upload and require explicit source IP networks. Use the assigned
Mac's Headscale `/32` and `/128` addresses, rather than authorizing every cluster. Private Mac
publication grants also require those source networks. A public read grant explicitly sets
`public_download: true` and has no private source-network restriction: its short-lived SSH key
is the user's read authority. It cannot be substituted into an agent input-fetch command.
Neither private keys nor staging tokens belong in job or cluster YAML.

Uploads land in mode-0600 temporary files under
`jobs/<job-UUID>/<transfer-UUID>/<kind>`. Excess, missing, and corrupt bytes are rejected before
publication. A SQLite transaction fences revocation at atomic rename; directory fsync and a
durable receipt follow. Verification hashes every file again and freezes the upload. Downloads
check the frozen source before sending bytes. The Mac independently checks SHA-256 before
acknowledging JOB_FETCH and again before enqueueing.

Repeated registration accepts the same immutable scope and can extend credential expiry.
It rejects conflicting manifests, key reuse, and revoked scopes. Startup and periodic sweeps
repair the atomic authorized-keys projection. Expired retention first commits revocation and
then deletes payload directories; a busy receiver or interrupted deletion retries later.

## HTTP API

The following private routes require the distinct scheduler control credential in an
`Authorization: Bearer …` header:

| Method and path | Purpose |
| --- | --- |
| `PUT /internal/transfers` | Register a versioned grant, retry safely |
| `GET /internal/transfers/<UUID>` | Inspect metadata and persisted state |
| `DELETE /internal/transfers/<UUID>` | Revoke the scope and remove projected keys |
| `POST /internal/transfers/<UUID>/verify` | Hash and seal a Mac export under scheduler control |

The public `POST /api/uploads/<UUID>/verify` requires that upload's staging token in the
same header. The gateway hashes it and compares against the registered digest; it never
stores the token. A control credential cannot substitute for an upload credential.
No artifact bytes pass through these HTTP routes.

The distinct private sealing route is used by the scheduler after an acknowledged Mac upload.
It verifies the stored manifest and actual bytes, then freezes the source. The Mac's SSH identity
authenticates publication; it receives neither a gateway control secret nor a public staging token.
The scheduler checks the returned manifest before making a read ticket available.

## Linux deployment

Use a dedicated `fl-transfer` account with a normal login shell, a protected home/state directory
at `/var/lib/fl-transfer`, and mode 0700 on that directory. Keep the application and BBCP
installation owned by the deployment administrator. The SSH account executes only per-key
forced commands generated by the service. Do not add ordinary SSH keys to its authorized file.
The parent directories must satisfy OpenSSH StrictModes checks.

Generate an Ed25519 host key for the dedicated SSH daemon and distribute its canonical public
key through trusted scheduler endpoint metadata. Do not use automatic host-key acceptance.
Give both scheduler and gateway the same random control credential using their protected
service configuration. The gateway reads it from a mode-0600 file owned by `fl-transfer`.

Example `/etc/fl/transfer-gateway.env`:

```ini
FL_GATEWAY_ROOT=/var/lib/fl-transfer
FL_GATEWAY_BBCP=/opt/fl-tools/bbcp
FL_GATEWAY_CONTROL_SECRET_FILE=/var/lib/fl-transfer/control-secret
FL_GATEWAY_DATA_PORT_FIRST=5000
FL_GATEWAY_DATA_PORT_LAST=5099
FL_BIND_HOST=127.0.0.1
FL_BIND_PORT=8081
```

Render `services/transfer-gateway/nginx.conf` and `sshd.conf` with distinct public/private
addresses using the [Linux deployment renderer](linux-deployment.md), which also supplies
pinned endpoint files and matching data-port environment defaults. Install them with valid
certificates. Public HTTPS exposes verification only; private HTTPS exposes
control only. The dedicated SSH daemon listens on gateway addresses, restricts authentication
to public keys, disables forwarding/TTY/user RC, and provides no SFTP subsystem. Validate its
rendered configuration with `sshd -t -f ...` before installation. If the normal administrative
SSH daemon uses port 22, bind it to different addresses so the listeners do not conflict.

Install `fl-transfer-gateway.service` after the Python distribution and BBCP are available.
The service writes only its state root and private temporary directory. Start the separately
configured SSH daemon under the host's service manager. This repository does not install or
alter live SSH services automatically.

Allow public TCP 443, the dedicated SSH port, and gateway BBCP ports 5000–5099. The shipped
Headscale ACL permits cluster-to-gateway traffic on these ports and scheduler-to-gateway
HTTPS; no scheduler SSH execution is required. Keep any custom endpoint/daemon port range
consistent with host firewall rules and Headscale ACLs. Obtain private HTTPS certificates
through DNS validation and ensure Mac agents resolve the private gateway address.

## Build and acceptance

The official source is pinned to commit `866ba250b8e7915f23e159a1060038933e829780` and archive
SHA-256 `103292abfc1175285e512fb4cfbb01334d8c208b13b821a244c3de6884a1344a`. The helper authenticates
the archive before extraction; it retains upstream source and license files in the build directory.
Linux requires a C/C++ compiler, make, OpenSSL, zlib and libnsl development libraries:

```sh
pixi run python tests/build_bbcp.py --directory /tmp/fl-bbcp
FL_TEST_BBCP=/tmp/fl-bbcp/bbcp pixi run test
```

On a Mac, install Apple's command-line tools and OpenSSL, then pass its prefix explicitly:

```sh
pixi run python tests/build_bbcp.py --directory /tmp/fl-bbcp \
  --openssl-prefix /opt/homebrew/opt/openssl@3
```

The helper selects native Darwin/arm64 compilation with pthread semaphores and avoids upstream's
obsolete deployment target. The Mac build and actual Mac transfer still require validation on a
Mac; Linux verification does not establish them. The gateway's server components target Linux.

Linux CI builds BBCP and runs a disposable loopback SSH daemon. Integration tests exercise
upload, freeze, scoped pull, spaces in local/state paths, incorrect host keys, corrupt bytes,
control/staging authority separation, dangerous command rejection, revocation races, and
retention deletion after reopening the store. The temporary SSH fixture relaxes StrictModes
because `/tmp` is outside the account's protected home; the production configuration keeps it
enabled. [Combined private-network acceptance](private-network-acceptance.md) now carries
production transfers through actual Headscale/WireGuard peers, preserves assigned source
addresses for native SSH checks, and exercises private HTTPS/WSS plus terminal result retrieval
with normal paths and forced local DERP transport.
Those userspace Linux fixtures do not establish the native Mac build or socket/DNS path.
Concurrent native acceptance runs eight independent public clients, each uploading two distinct
artifacts, with both an eight-port range and the default range. Every manifest and payload is
verified separately. This reproduced receiver bind failures before per-port reservations were
added. Separate real-socket/process tests verify bounded waits and lock release after SIGKILL.
Additional acceptance uploads through real BBCP, resumes scheduler delivery from PostgreSQL
after worker restart, fetches over a live agent WebSocket, and runs mock hardware only after
verified bytes. Tests cover transient fetch retry, authenticated public delivery, stale-session
ACK fencing, and cancellation while an injected slow payload operation is active.
Public terminal acceptance uses human authentication, obtains a reservation-bound upload grant,
uploads through real BBCP, verifies the gateway receipt, and executes on a connected mock agent.
Lost job-creation and delivery responses resume without duplicate uploads or execution.
The same acceptance downloads job outputs and original inputs through public BBCP, then reuses
verified local files on retry. Further tests cover download nonce replay/owner checks, changed or
corrupted files, no-clobber publication, export-worker replacement, sealing replay, cancellation
during a blocked export, deletion revocation, and revocation during an existing gateway stream.
