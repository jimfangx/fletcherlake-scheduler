# Private network execution and transfer acceptance

Linux integration scenarios combine the production agent/client/scheduler/gateway code
with checksum-pinned Headscale 0.29.4, Tailscale 1.102.4 userspace WireGuard peers and official
BBCP. They use mock hardware on the simulated Mac. Production Mac agents still use their native
Tailscale interfaces and launchd; none of the adapters below is installed with the platform.
The transfer and terminal scenarios run with normal path selection and with both IPv4/IPv6 UDP
sockets disabled, requiring the local DERP relay. Relay-only checks require an empty direct
address, the expected relay region, and positive transmit/receive counters for each exercised
private service pair.

```sh
pixi run python tests/download_network_tools.py --directory /tmp/fl-network
pixi run python tests/build_bbcp.py --directory /tmp/fl-bbcp
FL_TEST_HEADSCALE=/tmp/fl-network/headscale \
FL_TEST_TAILSCALE_DIR=/tmp/fl-network/tailscale_1.102.4_amd64 \
FL_TEST_BBCP=/tmp/fl-bbcp/bbcp \
pixi run pytest -q tests/integration/test_private_bbcp.py \
  tests/integration/test_private_terminal.py tests/integration/test_headscale_restart.py
```

Install gcc, OpenSSH server and the BBCP build prerequisites first. Existing Linux CI installs
these tools and enables these scenarios. Native socket adapters explicitly skip on macOS; the
existing Mac agent/SDK tests keep running there. Missing configured binaries fail rather than
substituting an implementation. No public provider credentials or BWRC endpoints are used.

## What the scenarios prove

The transfer/source-fence scenario enrolls a gateway and two separately tagged clusters under
the shipped policy. A normal public SSH/BBCP client uploads ELF and bitstream without using a
private proxy. After gateway SHA verification, an agent stages its own protected identity and
fetches both files through its actual private peer. The native gateway SSH daemon accepts the
assigned cluster's `/32` and rejects the same valid key from the other cluster. The test first
establishes both clusters' permitted gateway paths, so a cold or denied VPN connection cannot
stand in for the SSH source restriction. The rejected attempt must create a new source record
and report native public-key denial. The agent independently verifies both files before
mock execution, and records one verification event.

The terminal scenario enrolls distinct cluster, scheduler and gateway peers. Scheduler-to-gateway
control uses actual private HTTPS. The agent connects outbound over private WSS using its
ordinary HELLO/snapshot/ACK protocol. Certificates include the allocated private IPs and are
verified against the fixture CA with hostname checking enabled. Private transfer grants use the
registered cluster's actual Headscale address, without `simulation_networks` overrides.

The terminal uses the ordinary shared `Submission` and `Results` workflows, protected credentials
and receipt files, and public BBCP endpoints. Public HTTP routes and Google authorization use
in-process test servers/injected identity providers. It submits both inputs, waits for the
scheduler's replicated success, exports retained outputs and original inputs, downloads them
through public read scopes, and replays its delivered submission without another execution.
The running event must appear exactly once. Private native-socket traces show both the SSH
bootstrap and parallel BBCP payload connections targeting the gateway's real private address.

The restart scenario stops the real coordinator and its embedded relay while two boards run
and two jobs wait in a local queue. Direct UDP is disabled, so surviving direct connections
cannot hide the outage. Private HTTPS becomes unavailable and the agent detects its lost WSS
connection. Running mock hardware remains active; another board completes offline without
advancing the scheduler's event cursor. A scheduler cancellation remains pending until reconnection.

Restart uses the same database, Noise key and relay key. The three node identities and peer
processes must survive without re-enrollment. The agent reconnects under a new scheduler session,
projects offline completion once, cancels queued work before execution and finishes the surviving
queue once. Internal private DNS queries must return the scheduler and gateway addresses before,
during and after the outage. This checks the peer resolver and cached network map; the fixture
disables host DNS installation. A protected `headscale-restart-report.json` records elapsed outage
time and job identities without credentials. This scenario uses input-free jobs; interruption
of an active BBCP transfer is covered separately by transfer tests.
Exact event counts are checked after the scheduler's durable cursor catches up with the agent.
Job metadata from a snapshot can become visible before the corresponding event transaction commits.

These scenarios complement the existing [public proxy checks](linux-deployment.md),
[peer/port ACL isolation](headscale-operations.md), [load/outage checks](scheduler-acceptance.md)
and [transfer revocation/integrity tests](transfer-gateway.md).
Separate public-only native acceptance also exercises eight concurrent SSH/BBCP clients with
independent manifests and checks gateway port reservations under the smallest supported and
default ranges. These reservations stay within the deployed ACL range and grant deadline.

## How the Linux harness preserves source identity

The disposable Headscale process also runs its actual embedded DERP server, verifies enrolled
clients, and advertises a loopback-only relay map. Its STUN listener uses a temporary UDP port.
The peers use Tailscale's `TS_DEBUG_USE_DERP_HTTP` test knob for that loopback relay; the forced
relay variants additionally set `TS_DEBUG_ALWAYS_USE_DERP`. WireGuard still encrypts peer
packets. These knobs apply only to fixture daemon environments and do not change deployment
TLS configuration. A relay descriptor without a listening server left the earlier fixture
dependent on direct UDP discovery.
Enrollment waits for the local relay selection, and service readiness waits for the destination's
advertised relay route before attempting TCP. Tailscale's `Running` backend state alone does
not establish either condition. An encrypted TSMP ping confirms the actual peer path before
the service probe. Transfer readiness uses HTTP CONNECT to the actual SSH port and reads its
native banner, consuming exactly the proxy header so buffering cannot discard the banner.
The terminal scenarios separately verify the actual private HTTPS listener. Protected
transport-only logs survive peer teardown; control and authentication output is excluded.
Protected per-peer JSON snapshots also retain selected public transport fields and TCP packet
filters, so a stale receiver policy can be distinguished from SSH source rejection. The full
network map, node/machine keys and user profiles are excluded. Readiness failures identify the
originating peer. Linux CI retains only these filtered artifacts and the restart report for seven
days; raw fixture directories containing credentials are excluded from artifact upload.

The live-enrollment regression adds two clusters after an existing gateway has served private
traffic. Each new cluster must reach its permitted TCP listener through the relay while the
gateway daemon remains running. The existing source-fence scenario separately adds a second
cluster before opening listeners and checks native SSH authorization from both sources.

This host cannot create a TUN device. The test peers therefore use userspace WireGuard, with
their outbound HTTP proxies and private TCP forwarding. A Linux-only `connect` adapter routes
only `100.64.0.0/10` native SSH/BBCP connections through the originating peer's HTTP CONNECT
proxy. Other addresses retain their normal system behavior. CONNECT parsing consumes exactly
the HTTP header so it cannot swallow an SSH banner; deadlines and process-group cleanup bound
every transfer. gcc builds the short adapter sources under the test's temporary directory.

A userspace TCP forwarder normally replaces the peer source with loopback. The pinned gateway
peer can supply PROXY-v1 metadata, so a loopback inetd bridge passes its real WireGuard source
address into the native SSH daemon's socket view. OpenSSH itself applies the unchanged
`authorized_keys` `from=` restriction and fixed forced command. The fixture restores peer
metadata rather than changing the grant to permit loopback or all clusters. The destination
in that proxy header refers to the backend; it is not used as grant authority.

The adapter environment exists only for the fixture's native client subprocesses and its
inetd SSH child. The production gateway guard still creates its restricted BBCP environment.
No production connection code, grant scope, host-key checks or MAC queue logic is replaced.
The private SSH bridge closes accepted sockets, kills and reaps its own child groups, and
joins its handler threads on teardown. Diagnostics and connection traces remain protected
temporary files; traces contain addresses/ports, not credentials or payloads.

## Remaining native and operational gates

Userspace socket adaptation does not prove a native macOS TUN/socket path, Mac DNS configuration,
sleep/wake behavior or a Darwin BBCP build. Public DNS/certificate issuance, systemd service boot,
production DERP over HTTPS and real NAT/firewall conditions, physical firmware and live
provider setup are separate gates. BWRC checkout/release applies only to deployments that
enable the optional license relay. Keep them visible in
[the requirement ledger](implementation-status.md).
