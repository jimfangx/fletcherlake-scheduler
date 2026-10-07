# rclone transfer gateway

The Linux gateway receives large collateral separately from scheduler metadata. The shared
`fl_common.rclone.Rclone` client runs on a user's computer or a Mac agent. It pins the gateway's
Ed25519 SSH host key, uses a separate protected identity for each transfer, and terminates the
whole process group at the credential deadline. Mac agents initiate downloads; the gateway
listens for their rclone/SFTP connections through Headscale. Macs need no inbound SSH server.

The transport, gateway APIs, public reserved upload grants, protected terminal receipts,
scheduler delivery worker, and Mac fetch commands are implemented. `fl-client submit` automates
both transfer hops and resumes the same job after lost responses. `fl-client results` exports
terminal artifacts from the Mac and downloads independently verified files through public read
scopes. See [the terminal workflows](remote-client.md).

## Protocol and scope

The shared client runs `rclone copyto` with the [SFTP backend](https://rclone.org/sftp/)
and external OpenSSH. All payload bytes travel inside encrypted SSH, including the public
user-to-gateway hop. Private Mac traffic also traverses Headscale. Only the dedicated SSH
port (normally TCP 22) is required for file transfer. HTTPS remains the metadata/control path.
Macs initiate both downloads and publication; no incoming Mac SSH or SFTP service is needed.

Every authorized key forces the fixed gateway guard and a single transfer UUID. The guard
accepts only the `internal-sftp` subsystem request. Its SFTP v3 server exposes the virtual
`/transfer/<UUID>/<kind>` paths in that immutable manifest, with only the permitted read or
write direction. It rejects arbitrary commands, traversal, other grants, links, removal,
renames and remote shell/hash commands. The gateway does not expose the host filesystem or
run a general SFTP server as the transfer account.

For large files, rclone pipelines up to 64 requests of 240 KiB per encrypted connection
(15 MiB outstanding). Each copy transfers one file; independent grants can run
concurrently through the same SSH listener. Chunks leave room for packet headers below the
rclone SFTP library's complete-response limit of 256 KiB. Remote hash commands, timestamp updates,
range streams and alternate remote filenames are disabled because the gateway accepts only
exact manifest paths. Independent SHA-256 verification remains authoritative.
This configuration is not a measured production throughput guarantee: disks, CPU encryption,
latency and the existing ten-minute credential deadline still bound a transfer.

A protected temporary rclone configuration and known-hosts file isolate transfers from user
remotes and `RCLONE_*` settings. OpenSSH uses only the supplied identity, pinned host key and
fixed options. Per-grant filesystem locks serialize payload mutation. The SFTP server checks
revocation, expiry and source state on each request, bounds write offsets/lengths to declared
sizes, and discards incomplete temporary files on disconnect. Upload close hashes and
atomically publishes the completed file; corrupt or missing bytes never become collateral.

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
at `/var/lib/fl-transfer`, and mode 0700 on that directory. Keep the application
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
FL_GATEWAY_CONTROL_SECRET_FILE=/var/lib/fl-transfer/control-secret
FL_BIND_HOST=127.0.0.1
FL_BIND_PORT=8081
```

Render `services/transfer-gateway/nginx.conf` and `sshd.conf` with distinct public/private
addresses using the [Linux deployment renderer](linux-deployment.md), which also supplies
pinned endpoint files and SSH-only ACLs and environment defaults. Install them with valid
certificates. Public HTTPS exposes verification only; private HTTPS exposes
control only. The dedicated SSH daemon listens on gateway addresses, restricts authentication
to public keys and disables forwarding/TTY/user RC. Its configured `internal-sftp` subsystem
request is replaced by each key's forced manifest-scoped guard; it grants no general SFTP access. Validate its
rendered configuration with `sshd -t -f ...` before installation. If the normal administrative
SSH daemon uses port 22, bind it to different addresses so the listeners do not conflict.

Install `fl-transfer-gateway.service` after the Python distribution and OpenSSH are available.
The gateway needs no rclone binary; only clients and Macs run it.
The service writes only its state root and private temporary directory. Start the separately
configured SSH daemon under the host's service manager. This repository does not install or
alter live SSH services automatically.

Allow public TCP 443 and the dedicated SSH port, normally 22. The shipped Headscale ACL
permits cluster-to-gateway TCP 22 and scheduler-to-gateway HTTPS; no scheduler SSH execution
is required. Keep any custom SSH listener port consistent with endpoint metadata, host/cloud
firewalls and Headscale ACLs. Obtain private HTTPS certificates through DNS validation and
ensure Mac agents resolve the private gateway address.

## Installation and acceptance

The installer downloads official rclone **v1.75.1** binaries and verifies a pinned SHA-256
before extracting the binary. It supports Linux x86-64/arm64 and macOS Apple Silicon/Intel.
macOS 12 or later is required by the pinned release. No local compilation or OpenSSL
installation is needed. Install on each user host and Mac, from the reviewed checkout:

```sh
pixi run python tests/download_rclone.py --directory /tmp/fl-rclone
sudo install -d -m 755 /opt/fl-tools
sudo install -m 755 /tmp/fl-rclone/rclone /opt/fl-tools/rclone
/opt/fl-tools/rclone version
```

For a Mac, set `environment.rclone.path: /opt/fl-tools/rclone` in cluster overrides.
For a rootless user installation, use the commands in
[the deployment guide](deployment-guide.md#11-remote-client). The installer pins these archive
checksums from the [official release](https://github.com/rclone/rclone/releases/tag/v1.75.1):

| Archive | SHA-256 |
| --- | --- |
| `rclone-v1.75.1-linux-amd64.zip` | `982b5aa772841168f8e380f139e9e787b2a105403e32b94da8676a0e1c0a13ab` |
| `rclone-v1.75.1-linux-arm64.zip` | `03f2504174034b6d004152ed7369251c9a9ec1f7e0836eda420f5c7a5ec0dff9` |
| `rclone-v1.75.1-osx-amd64.zip` | `29253d0288b8fbbac46baad6e5f6add6cb01d462c79f10805bbd4631c4cdf82c` |
| `rclone-v1.75.1-osx-arm64.zip` | `c61d7a371c62bcbbe882c3423aa4b8bf63485c248dd0f692997b8f0c3f6d0c6f` |

Linux CI downloads the verified rclone binary and runs a disposable OpenSSH daemon:

```sh
pixi run python tests/download_rclone.py --directory /tmp/fl-rclone
FL_TEST_RCLONE=/tmp/fl-rclone/rclone pixi run test
```

Integration tests exercise upload, freeze, scoped pull, paths with spaces, incorrect host keys,
corrupt bytes, control/staging authority separation, revocation and retention. Protocol tests
cover virtual paths, manifest/direction boundaries, pipelined out-of-order writes, declared-size
limits, disconnect cleanup and unsupported operations. The temporary SSH fixture relaxes
StrictModes because `/tmp` is outside the account's protected home; production keeps it enabled.
[Combined private-network acceptance](private-network-acceptance.md) carries production
transfers through actual Headscale/WireGuard peers, preserves assigned source addresses for
native SSH checks, and exercises private HTTPS/WSS plus terminal result retrieval with normal
paths and forced local DERP transport. Those Linux fixtures do not establish native Mac transfer,
DNS or socket behavior. Concurrent native acceptance runs eight independent public clients,
each uploading two distinct artifacts, through the same dedicated SSH listener; every manifest
and payload is verified separately.

Additional acceptance uploads through real rclone/SFTP, resumes scheduler delivery from PostgreSQL
after worker restart, fetches over a live agent WebSocket, and runs mock hardware only after
verified bytes. Tests cover transient fetch retry, authenticated public delivery, stale-session
ACK fencing, and cancellation while an injected slow payload operation is active.
Public terminal acceptance uses human authentication, obtains a reservation-bound upload grant,
uploads through real rclone/SFTP, verifies the gateway receipt, and executes on a connected mock agent.
Lost job-creation and delivery responses resume without duplicate uploads or execution.
The same acceptance downloads job outputs and original inputs through public rclone/SFTP, then reuses
verified local files on retry. Further tests cover download nonce replay/owner checks, changed or
corrupted files, no-clobber publication, export-worker replacement, sealing replay, cancellation
during a blocked export, deletion revocation, and revocation during an existing gateway stream.

## Migrating an existing BBCP deployment

Roll out a reviewed matching revision to the gateway, scheduler, Macs and user clients.
Existing BBCP clients cannot use the new SFTP guard. Drain active transfers/jobs before the
coordinated update so an old transfer is not interrupted midway through publication.

1. Install the pinned rclone binary on each user host and Mac. Replace
   `environment.bbcp` overrides with `environment.rclone`, including the new executable path.
   Run Mac setup reconfiguration/confirmation from the new checkout and verify readiness.
   Legacy inventory fields are ignored when reading existing configuration; they do not
   install rclone or substitute a BBCP executable.
2. Remove `bbcp_binary` and gateway `data_port_first`/`data_port_last` from deployment inventory.
   Re-render/install endpoint JSON, gateway environment, SSH configuration and Headscale policy.
   Remove `FL_GATEWAY_BBCP` and `FL_GATEWAY_DATA_PORT_*` from manually managed environment files.
   Private gateway file-transfer ACLs now permit only TCP 22 (or your explicit SSH port).
   The obsolete gateway-to-Mac transfer rule is removed; Macs initiate every SFTP connection.
3. Restart the gateway HTTP service and dedicated SSH daemon. Startup regenerates the
   authorized-keys projection with the new SFTP guard. Keep the state database, payload
   directories, control credential and SSH host key; the existing grant schema and retention
   behavior need no data migration. Legacy endpoint data-port fields are ignored on read.
4. Close the old 5000–5099 payload range in cloud and host firewalls; use
   [the AWS/GCP commands](deployment-guide.md#upgrading-an-existing-transfer-gateway).
   Test submission, private Mac fetch, result publication and user results download before
   resuming production traffic. Verify both direct and DERP fallback private paths.

The gateway still retains files until their configured retention deadline or explicit
revocation/cleanup. A completed Mac download does not delete the gateway copy.
