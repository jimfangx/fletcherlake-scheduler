"""Production templates pass real Linux parsers with disposable filesystem paths."""

import os
import shutil

import pytest
import yaml
from fl_deploy.manifest import Deployment
from fl_deploy.native import substitute
from fl_deploy.render import render, write_bundle

from tests.deployment import (
    TEMPLATES,
    certificates,
    inventory,
    nginx_config,
    require_native_tools,
    run,
)
from tests.nginx_runtime import free_port


@pytest.mark.parametrize("license_relay", [False, True], ids=["without-relay", "with-relay"])
def test_rendered_files_pass_all_native_parsers(tmp_path, license_relay):
    nginx, haproxy, headscale = require_native_tools()
    data = inventory(tmp_path, license_relay=license_relay)
    checkout = tmp_path / 'checkout % "quoted"'
    checkout.symlink_to(TEMPLATES.parent, target_is_directory=True)
    data.update(
        install_root=str(checkout),
        headscale_binary=headscale,
        certificate_root=str(tmp_path / 'certificates "quoted"'),
    )
    manifest = Deployment.model_validate(data)
    certificates(tmp_path, manifest)
    files = render(manifest, TEMPLATES)
    bundle = tmp_path / "bundle"
    write_bundle(files, bundle)
    port = free_port()
    # nginx -t opens listeners. CI has no production interfaces; map each address
    # to a distinct loopback alias and use an unprivileged port for this fixture.
    bindings = {
        f"listen {address}:443 ssl": f"listen 127.0.0.{index}:{port} ssl"
        for index, address in enumerate(
            (
                manifest.scheduler.public_ip,
                manifest.scheduler.private_ip,
                manifest.gateway.public_ip,
                manifest.gateway.private_ip,
            ),
            start=1,
        )
    }
    for role, names in (
        ("scheduler", ["headscale-nginx.conf", "scheduler-nginx.conf"]),
        ("gateway", ["transfer-gateway-nginx.conf"]),
    ):
        root = tmp_path / role
        root.mkdir(mode=0o700)
        config = nginx_config(
            root, [substitute((bundle / role / name).read_text(), bindings) for name in names]
        )
        run(nginx, "-p", str(root), "-c", str(config), "-t")
    if license_relay:
        run(haproxy, "-c", "-f", str(bundle / "license-relay/haproxy.cfg"))
    sshd = shutil.which("sshd") or "/usr/sbin/sshd"
    config = bundle / "gateway/sshd.conf"
    config.write_text(
        config.read_text().replace("/etc/fl/transfer-host-ed25519", str(tmp_path / "host.key"))
    )
    run(sshd, "-t", "-f", str(config))
    effective = run(sshd, "-T", "-f", str(config))
    for setting in ("disableforwarding yes", "permittty no", "authenticationmethods publickey"):
        assert setting in effective
    assert "subsystem " not in effective
    coordinator = yaml.safe_load((bundle / "scheduler/headscale.yaml").read_text())
    coordinator["unix_socket"] = str(tmp_path / "headscale.sock")
    coordinator["noise"]["private_key_path"] = str(tmp_path / "noise.key")
    coordinator["database"]["sqlite"]["path"] = str(tmp_path / "headscale.sqlite")
    coordinator["policy"]["path"] = str(bundle / "scheduler/policy.json")
    config = tmp_path / "headscale.yaml"
    config.write_text(yaml.safe_dump(coordinator))
    run(headscale, "--config", str(config), "configtest")
    run(
        headscale,
        "--config",
        str(config),
        "--force",
        "policy",
        "check",
        "--bypass-grpc-and-access-database-directly",
        "-f",
        str(bundle / "scheduler/policy.json"),
    )
    units = tmp_path / "units"
    units.mkdir(mode=0o700)
    for name in ("sysinit", "basic", "sockets", "network-online", "multi-user", "shutdown"):
        (units / f"{name}.target").write_text("[Unit]\nDescription=Isolated parser-check target\n")
    paths = []
    for source in bundle.rglob("*.service"):
        target = units / source.name
        target.write_text(source.read_text())
        paths.append(str(target))
    for name in ("nginx", "haproxy", "tailscaled"):
        unit = units / f"{name}.service"
        unit.write_text("[Service]\nExecStart=/usr/bin/true\n")
        paths.append(str(unit))
    for name, role in (("nginx", "scheduler"), ("haproxy", "license-relay")):
        if role != "license-relay" or license_relay:
            shutil.copytree(bundle / role / f"{name}.service.d", units / f"{name}.service.d")
    run(
        "systemd-analyze",
        "verify",
        "--man=no",
        *paths,
        env={**os.environ, "SYSTEMD_UNIT_PATH": str(units)},
    )
