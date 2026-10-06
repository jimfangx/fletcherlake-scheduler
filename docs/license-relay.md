# BWRC license relay

License forwarding is optional. For the current Vivado Lab setup on the Macs, leave
`license_relay: null` in the deployment inventory or omit the field entirely. The renderer
omits the relay bundle, HAProxy drop-in, private license DNS record and Headscale license
ACL/tag. Scheduler, gateway, enrollment and Mac execution do not require a BWRC relay.

To enable forwarding for tools that need it, add a complete mapping to the deployment YAML:

```yaml
license_relay:
  private_ip: 100.64.0.30
  backend_ip: 192.0.2.30
  hostname: vivado-license.example.edu
  manager_port: 2100
  vendor_port: 2101
```

Replace these example addresses, name and ports with authorized endpoints. An empty or partial
mapping is rejected; use `null` to disable it. Existing manifests with a complete mapping keep
forwarding enabled. To disable a previously installed relay, install the newly rendered
Headscale DNS/policy and stop/remove its old HAProxy configuration separately; rendering does
not alter running services.

Use a dedicated BWRC-connected infrastructure host tagged `tag:license-relay`. It forwards
only specific license TCP endpoints from its Headscale address to the BWRC license server.
It does not advertise the BWRC subnet, act as an exit node, or expose a public listener.
Human Chipyard machines do not need to join Headscale to use the platform.

`services/license-relay/haproxy.cfg` supplies separate TCP frontends for the Vivado license
manager and vendor daemon. Install HAProxy on the relay and render its placeholders with
the relay's allocated Headscale IP and the authorized BWRC server's IP. Bind to the private
IP explicitly. Run `haproxy -c -f /PATH/haproxy.cfg` before loading it through the host's
HAProxy service. Permit BWRC egress only to that server and those ports in the host firewall.

The sample uses 2100 for the license manager and 2101 for a **fixed** vendor-daemon port.
BWRC administrators must supply the actual endpoints and configure a fixed vendor-daemon
port. Update both HAProxy and `services/headscale/policy.json` together if the ports differ.
Frontend and backend port numbers should agree because the manager can tell the client which
vendor port to contact. A random vendor port cannot be served by this bounded policy.
Additional Cadence/Synopsys servers need their own explicit listeners and ACL destinations;
do not replace the rules with unrestricted subnet access.

Configure the Mac's license client with its licensed server hostname resolving to the relay's
private IP, and the agreed `port@hostname` value. Ensure any hostname advertised by the
license manager also resolves to that relay from the Mac. Keep the relay's backend address
as the real BWRC IP so that this DNS override cannot point the relay back at itself. Headscale
extra DNS records can provide this infrastructure-only override. Obtain the exact hostname
and vendor behavior from the BWRC license administrator rather than guessing them.
AMD's [network-license setup instructions](https://docs.amd.com/r/2024.1-English/ug973-vivado-release-notes-install-license/Serve-New-License-Servers)
describe the manager/license-file configuration.

When forwarding is enabled, acceptance requires a Vivado checkout and release from an enrolled
Mac, a vendor-port
connection, and a denied connection to another BWRC host/port. Test the same path after a
relay restart. No BWRC endpoints, license entitlement, or Mac are available in this development
environment, so that acceptance remains outstanding. The HAProxy file is a deployment
template. The [Linux deployment renderer](linux-deployment.md) now renders its ports together
with Headscale ACLs and private DNS. Native HAProxy parsing and a disposable two-port TCP relay
are tested, including private-interface binding and denied interface/port access. This has not
been run against a BWRC license server.
