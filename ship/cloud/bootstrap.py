"""One-time GCP setup for main deployments, with manual GitHub settings or authenticated gh."""

import argparse
import json
import os
import shlex
import shutil
import subprocess
import urllib.request
from pathlib import Path
from typing import Any


def call(argv: list[str], *, data: str | None = None, capture: bool = False) -> str:
    result = subprocess.run(
        argv, input=data, text=True, check=True, stdout=subprocess.PIPE if capture else None
    )
    return result.stdout if capture else ""


def exists(argv: list[str]) -> bool:
    return (
        subprocess.run(argv, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0
    )


def gcloud(*args: str, capture: bool = False) -> str:
    return call(["gcloud", *args, "--quiet"], capture=capture)


def describe(*args: str) -> dict[str, Any]:
    result: dict[str, Any] = json.loads(gcloud(*args, "--format=json", capture=True))
    return result


def configure(args: argparse.Namespace) -> None:
    executables = ["gcloud", "ssh-keygen"] + ([] if args.manual_github else ["gh"])
    for executable in executables:
        if not shutil.which(executable):
            raise SystemExit(f"Install {executable} before running cloud bootstrap")
    # Authenticate and inspect existing infrastructure before creating cloud resources.
    project = describe("projects", "describe", args.project)
    if args.manual_github:
        request = urllib.request.Request(
            f"https://api.github.com/repos/{args.repo}",
            headers={"User-Agent": "fl-cloud-bootstrap"},
        )
        with urllib.request.urlopen(request, timeout=30) as response:
            repository = json.load(response)
    else:
        call(["gh", "auth", "status"])
        repository = json.loads(call(["gh", "api", f"repos/{args.repo}"], capture=True))
    if repository["default_branch"] != "main":
        raise SystemExit("This deployment is restricted to the main branch")
    if not args.manual_github and not repository.get("permissions", {}).get("admin"):
        raise SystemExit("GitHub repository admin access is required to configure production")
    common = describe("compute", "project-info", "describe", f"--project={args.project}")
    project_metadata = {
        x["key"]: x["value"] for x in common.get("commonInstanceMetadata", {}).get("items", [])
    }
    targets = [("scheduler", args.scheduler, 22), ("gateway", args.gateway, 2222)]
    instances = {}
    for role, vm, _ in targets:
        instance = describe(
            "compute",
            "instances",
            "describe",
            vm,
            f"--project={args.project}",
            f"--zone={args.zone}",
        )
        metadata = {x["key"]: x["value"] for x in instance.get("metadata", {}).get("items", [])}
        if (
            metadata.get("enable-oslogin", project_metadata.get("enable-oslogin", "false")).lower()
            == "true"
        ):
            raise SystemExit(
                "This bootstrap requires the existing metadata SSH setup with OS Login disabled"
            )
        instances[role] = instance
    state = args.state.expanduser().resolve()
    state.mkdir(parents=True, exist_ok=True, mode=0o700)
    key = state / "github-deploy-ed25519"
    if not key.exists():
        call(["ssh-keygen", "-t", "ed25519", "-N", "", "-C", "fl-github-main", "-f", str(key)])
    key.chmod(0o600)
    public_key = key.with_suffix(".pub").read_text().strip()
    # Repository/owner numeric IDs prevent name reuse from inheriting federation access.
    condition = (
        f"assertion.repository_id == '{repository['id']}' && "
        f"assertion.repository_owner_id == '{repository['owner']['id']}' && "
        "assertion.ref == 'refs/heads/main' && assertion.event_name == 'push' && "
        f"assertion.workflow_ref == '{args.repo}/.github/workflows/ci.yml@refs/heads/main'"
    )
    gcloud(
        "services",
        "enable",
        "compute.googleapis.com",
        "iam.googleapis.com",
        "iamcredentials.googleapis.com",
        "sts.googleapis.com",
        "iap.googleapis.com",
        f"--project={args.project}",
    )
    service_account = f"fl-github-deploy@{args.project}.iam.gserviceaccount.com"
    if not exists(
        [
            "gcloud",
            "iam",
            "service-accounts",
            "describe",
            service_account,
            f"--project={args.project}",
        ]
    ):
        gcloud(
            "iam",
            "service-accounts",
            "create",
            "fl-github-deploy",
            "--display-name=Fletcherlake GitHub main deployment",
            f"--project={args.project}",
        )
    pool = ["iam", "workload-identity-pools"]
    scope = [f"--project={args.project}", "--location=global"]
    if not exists(["gcloud", *pool, "describe", "fl-github", *scope]):
        gcloud(*pool, "create", "fl-github", *scope, "--display-name=Fletcherlake GitHub")
    provider = [*pool, "providers"]
    provider_scope = [*scope, "--workload-identity-pool=fl-github"]
    operation = (
        "update-oidc"
        if exists(["gcloud", *provider, "describe", "main", *provider_scope])
        else "create-oidc"
    )
    gcloud(
        *provider,
        operation,
        "main",
        *provider_scope,
        "--issuer-uri=https://token.actions.githubusercontent.com",
        "--attribute-mapping=google.subject=assertion.sub,attribute.repository_id=assertion.repository_id",
        f"--attribute-condition={condition}",
    )
    pool_name = (
        f"projects/{project['projectNumber']}/locations/global/workloadIdentityPools/fl-github"
    )
    principal = (
        f"principalSet://iam.googleapis.com/{pool_name}/attribute.repository_id/{repository['id']}"
    )
    gcloud(
        "iam",
        "service-accounts",
        "add-iam-policy-binding",
        service_account,
        f"--project={args.project}",
        f"--member={principal}",
        "--role=roles/iam.workloadIdentityUser",
    )
    role_args = ["iam", "roles"]
    operation = (
        "update"
        if exists(
            ["gcloud", *role_args, "describe", "flCloudDeployLookup", f"--project={args.project}"]
        )
        else "create"
    )
    gcloud(
        *role_args,
        operation,
        "flCloudDeployLookup",
        f"--project={args.project}",
        "--title=Fletcherlake deploy instance lookup",
        "--stage=GA",
        "--permissions=compute.instances.get,compute.instances.list",
    )
    gcloud(
        "projects",
        "add-iam-policy-binding",
        args.project,
        f"--member=serviceAccount:{service_account}",
        f"--role=projects/{args.project}/roles/flCloudDeployLookup",
        "--condition=None",
    )
    clauses = [
        f"(destination.ip == '{instances[role]['networkInterfaces'][0]['networkIP']}' "
        f"&& destination.port == {port})"
        for role, _, port in targets
    ]
    gcloud(
        "projects",
        "add-iam-policy-binding",
        args.project,
        f"--member=serviceAccount:{service_account}",
        "--role=roles/iap.tunnelResourceAccessor",
        f"--condition=expression={' || '.join(clauses)},title=fl-cloud-management-ports",
    )
    hosts = []
    for role, vm, port in targets:
        tag, firewall = f"fl-ci-{role}", f"fl-ci-iap-{role}"
        network = instances[role]["networkInterfaces"][0]["network"].rsplit("/", 1)[-1]
        gcloud(
            "compute",
            "instances",
            "add-tags",
            vm,
            f"--project={args.project}",
            f"--zone={args.zone}",
            f"--tags={tag}",
        )
        operation = (
            "update"
            if exists(
                [
                    "gcloud",
                    "compute",
                    "firewall-rules",
                    "describe",
                    firewall,
                    f"--project={args.project}",
                ]
            )
            else "create"
        )
        firewall_args = (
            []
            if operation == "update"
            else [f"--network={network}", "--direction=INGRESS", "--action=ALLOW"]
        )
        gcloud(
            "compute",
            "firewall-rules",
            operation,
            firewall,
            f"--project={args.project}",
            *firewall_args,
            "--source-ranges=35.235.240.0/20",
            f"--target-tags={tag}",
            f"--rules=tcp:{port}",
        )
        # Existing authenticated administrator access pins the real host key; no keyscan trust.
        command = "\n".join(
            [
                "set -eu",
                "id fl-deploy >/dev/null 2>&1 || "
                "sudo useradd --create-home --shell /bin/bash fl-deploy",
                "sudo install -d -m 700 -o fl-deploy -g fl-deploy /home/fl-deploy/.ssh",
                f"printf '%s\\n' {shlex.quote('restrict ' + public_key)} | "
                "sudo tee /home/fl-deploy/.ssh/authorized_keys >/dev/null",
                "sudo chown fl-deploy:fl-deploy /home/fl-deploy/.ssh/authorized_keys",
                "sudo chmod 600 /home/fl-deploy/.ssh/authorized_keys",
                "printf '%s\\n' 'fl-deploy ALL=(root) NOPASSWD: ALL' | "
                "sudo tee /etc/sudoers.d/fl-github-deploy >/dev/null",
                "sudo chmod 440 /etc/sudoers.d/fl-github-deploy",
                "sudo visudo -cf /etc/sudoers.d/fl-github-deploy >/dev/null",
                "sudo cat /etc/ssh/ssh_host_ed25519_key.pub",
            ]
        )
        host_key = gcloud(
            "compute",
            "ssh",
            f"{args.admin_user}@{vm}",
            f"--project={args.project}",
            f"--zone={args.zone}",
            f"--ssh-key-file={args.admin_key.expanduser()}",
            f"--ssh-flag=-p{port}",
            f"--command={command}",
            capture=True,
        )
        host_key = next(line for line in host_key.splitlines() if line.startswith("ssh-ed25519 "))
        hosts.append(f"fl-{role}-ci {host_key}")
    variables = {
        "FL_GCP_PROJECT": args.project,
        "FL_GCP_ZONE": args.zone,
        "FL_DOMAIN": args.domain,
        "FL_SCHEDULER_VM": args.scheduler,
        "FL_GATEWAY_VM": args.gateway,
        "FL_WORKLOAD_IDENTITY_PROVIDER": f"{pool_name}/providers/main",
        "FL_DEPLOY_SERVICE_ACCOUNT": service_account,
        "FL_DEPLOY_KNOWN_HOSTS": "\n".join(hosts) + "\n",
    }
    (state / "github-production-variables.json").write_text(json.dumps(variables, indent=2) + "\n")
    (state / "github-deploy-known-hosts").write_text(variables["FL_DEPLOY_KNOWN_HOSTS"])
    if args.manual_github:
        print(
            "GCP setup complete. Production variables:", state / "github-production-variables.json"
        )
        print(f"FL_DEPLOY_SSH_KEY secret value is in: {key}")
        print("Add these in GitHub Settings > Environments > production. See docs/cloud-cicd.md.")
        return
    # Create production without reviewers if absent; preserve any existing environment rules.
    api = f"repos/{args.repo}/environments/production"
    if not exists(["gh", "api", api]):
        call(["gh", "api", "--method", "PUT", api])
    for name, value in variables.items():
        call(
            ["gh", "variable", "set", name, "--repo", args.repo, "--env", "production"], data=value
        )
    call(
        ["gh", "secret", "set", "FL_DEPLOY_SSH_KEY", "--repo", args.repo, "--env", "production"],
        data=key.read_text(),
    )
    print("Production configured. Push the reviewed CI/CD files to main to enable deployments.")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--manual-github",
        action="store_true",
        help="Configure GCP only and write settings for manual GitHub UI setup (public repos)",
    )
    parser.add_argument("--project", default="fletcherlake")
    parser.add_argument("--zone", default="us-west2-a")
    parser.add_argument("--repo", default="jimfangx/fletcherlake-scheduler")
    parser.add_argument("--domain", default="fl.ucb.bar")
    parser.add_argument("--scheduler", default="fl-scheduler")
    parser.add_argument("--gateway", default="fl-transfer-gateway")
    parser.add_argument("--admin-user", default="ubuntu")
    parser.add_argument(
        "--admin-key", type=Path, default=Path("~/.local/state/fl-deploy/admin-ed25519")
    )
    parser.add_argument("--state", type=Path, default=Path("~/.local/state/fl-deploy"))
    args = parser.parse_args()
    os.umask(0o077)
    configure(args)


if __name__ == "__main__":
    main()
