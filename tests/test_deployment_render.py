"""Deployment inventory rejects injection and keeps independent roles consistent."""

import json
import shutil

import pytest
import yaml
from fl_deploy.manifest import Deployment
from fl_deploy.render import load, render, write_bundle
from pydantic import ValidationError

from tests.deployment import TEMPLATES, inventory


def test_rendered_bundle_keeps_ports_dns_and_endpoints_consistent(tmp_path):
    data = inventory(tmp_path, license_relay=True)
    data["license_relay"].update(manager_port=2101, vendor_port=2100)
    data["gateway"].update(data_port_first=6100, data_port_last=6110)
    data["install_root"] = '/opt/fl deployment % "quoted"'
    data["certificate_root"] = '/etc/certificates with "quotes"'
    manifest = Deployment.model_validate(data)
    files = render(manifest, TEMPLATES)
    output = tmp_path / "bundle"
    write_bundle(files, output)
    assert load(output / "manifest.yaml") == manifest
    assert 'FL_GATEWAY_DATA_PORT_FIRST="6100"' in files["gateway/environment.defaults"]
    assert 'FL_GATEWAY_DATA_PORT_LAST="6110"' in files["gateway/environment.defaults"]
    assert all(path.stat().st_mode & 0o077 == 0 for path in output.rglob("*") if path.is_file())
    with pytest.raises(FileExistsError):
        write_bundle(files, output)
    relay = files["license-relay/haproxy.cfg"]
    assert "bind 100.64.0.30:2101" in relay
    assert "server manager 192.0.2.30:2101 check" in relay
    assert "server vendor 192.0.2.30:2100 check" in relay
    policy = json.loads(files["scheduler/policy.json"])
    assert policy["acls"][0]["dst"][-1] == "tag:license-relay:2101,2100"
    assert "tag:cluster:22,6100-6110" in policy["acls"][2]["dst"]
    assert "tag:transfer-gateway:22,6100-6110" in policy["acls"][3]["dst"]
    dns = yaml.safe_load(files["scheduler/headscale.yaml"])["dns"]["extra_records"]
    assert manifest.license_relay is not None
    assert dns[-1] == {"name": manifest.license_relay.hostname, "type": "A", "value": "100.64.0.30"}
    for name, host in (("public", manifest.gateway.hostname), ("private", "100.64.0.20")):
        endpoint = json.loads(files[f"gateway/{name}-endpoint.json"])
        assert endpoint["host"] == host
        assert endpoint["host_key"] == manifest.gateway.host_key
        assert (endpoint["data_port_first"], endpoint["data_port_last"]) == (6100, 6110)
    assert (
        'WorkingDirectory=/opt/fl deployment %% "quoted"' in files["scheduler/fl-scheduler.service"]
    )
    assert (
        'ssl_certificate "/etc/certificates with \\"quotes\\"/'
        in files["scheduler/scheduler-nginx.conf"]
    )


@pytest.mark.parametrize(
    "section,field,value",
    [
        ("scheduler", "public_ip", "0.0.0.0"),
        ("gateway", "private_ip", "192.168.1.10"),
        ("gateway", "public_ip", "100.64.0.20"),
        ("scheduler", "hostname", "evil.example;return 200;"),
        ("scheduler", "hostname", "*.example.edu"),
        ("scheduler", "hostname", "-bad.example.edu"),
        ("scheduler", "agent_hostname", "scheduler.example.edu"),
        ("license_relay", "private_ip", "100.64.0.10"),
        ("license_relay", "backend_ip", "100.64.0.30"),
        ("license_relay", "vendor_port", 2100),
        ("gateway", "data_port_last", 5006),
        ("gateway", "host_key", "ssh-ed25519 invalid"),
        ("gateway", "password", "never allowed"),
        (None, "install_root", "/opt/fl\nExecStart=/bin/false"),
        (None, "certificate_root", "/etc/$variable"),
        (None, "headscale_binary", "relative/headscale"),
        (None, "install_root", "/opt/../root"),
        (None, "install_root", "/opt/fl\\"),
        (None, "install_root", "/opt/fl "),
        (None, "google_client_secret", "never allowed"),
    ],
)
def test_manifest_rejects_unsafe_or_inconsistent_inventory(tmp_path, section, field, value):
    data = inventory(tmp_path, license_relay=True)
    target = data[section] if section else data
    target[field] = value
    with pytest.raises(ValidationError):
        Deployment.model_validate(data)


@pytest.mark.parametrize("omit", [False, True], ids=["null", "omitted"])
def test_deployment_without_license_relay(tmp_path, omit):
    data = inventory(tmp_path)
    if omit:
        del data["license_relay"]
    manifest = Deployment.model_validate(data)
    assert manifest.license_relay is None
    # Optional deployment must not even need the relay template to render.
    templates = tmp_path / "templates"
    shutil.copytree(TEMPLATES, templates, ignore=shutil.ignore_patterns("node_modules", "dist"))
    shutil.rmtree(templates / "license-relay")
    files = render(manifest, templates)
    output = tmp_path / "bundle"
    write_bundle(files, output)
    assert load(output / "manifest.yaml") == manifest
    assert not (output / "license-relay").exists()
    policy = json.loads(files["scheduler/policy.json"])
    assert "tag:license-relay" not in policy["tagOwners"]
    assert all(
        "license-relay" not in destination for rule in policy["acls"] for destination in rule["dst"]
    )
    assert policy["acls"][0]["dst"] == ["tag:scheduler:443", "tag:transfer-gateway:443"]
    dns = yaml.safe_load(files["scheduler/headscale.yaml"])["dns"]["extra_records"]
    assert dns == [
        {"name": manifest.scheduler.agent_hostname, "type": "A", "value": "100.64.0.10"},
        {"name": manifest.gateway.private_hostname, "type": "A", "value": "100.64.0.20"},
    ]
    assert "scheduler/fl-scheduler.service" in files
    assert "gateway/fl-transfer-gateway.service" in files
