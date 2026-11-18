# Cloud deployment from GitHub Actions

Pushes to `main` deploy the scheduler and transfer gateway after every job in
[the CI matrix](../.github/workflows/ci.yml) passes. Pull requests and other branches run
checks without production access. The two existing GCP VMs are the cloud targets:

| VM | Services |
| --- | --- |
| `fl-scheduler` | Scheduler, dashboard, PostgreSQL, Headscale, nginx |
| `fl-transfer-gateway` | Gateway HTTP API, dedicated transfer SSH server, nginx |

Application updates deploy the scheduler/dashboard and gateway code. PostgreSQL, Headscale,
nginx, TLS, mounted data, credentials and service configuration retain their existing
installation. Infrastructure/configuration changes use the operator deployment procedures.
Mac agents and user clients are updated separately. A breaking protocol/schema change needs
a coordinated rollout to those clients; CI cannot establish compatibility with physical boards.

## One-time setup

Use the operator host that has existing administrator SSH access to both VMs and its saved
`~/.local/state/fl-deploy/admin-ed25519`. Authenticate `gcloud` as a project administrator,
then run from the scheduler repository:

```bash
gcloud auth login
python3 ship/cloud/bootstrap.py --manual-github
```

This configures GCP and the two VMs without signing in to GitHub or changing repository
settings. For this public repository, it reads only public GitHub repository metadata.
It writes the nonsecret environment values to
`~/.local/state/fl-deploy/github-production-variables.json`, the pinned host keys to
`~/.local/state/fl-deploy/github-deploy-known-hosts`, and the private deployment SSH key to
`~/.local/state/fl-deploy/github-deploy-ed25519` (mode 0600).

In [the repository settings](https://github.com/jimfangx/fletcherlake-scheduler/settings/environments):

1. Open **Settings → Environments → New environment**, name it `production`, and create it.
   Set deployment branches to selected branches and add `main`. Leave required reviewers
   and wait timers disabled for automatic deployment after tests pass.
2. Under **Environment variables**, add each of the eight entries from
   `github-production-variables.json`. For the multiline `FL_DEPLOY_KNOWN_HOSTS` value,
   copy the raw contents of `github-deploy-known-hosts`, with actual newlines.
3. Under **Environment secrets**, add `FL_DEPLOY_SSH_KEY` with the entire contents of
   `github-deploy-ed25519`, including the BEGIN/END lines. On a Mac,
   `pbcopy < ~/.local/state/fl-deploy/github-deploy-ed25519` copies it without printing it.
4. Under **Settings → Actions → General**, ensure Actions and the actions used by
   `.github/workflows/ci.yml` are allowed. The workflow specifies its token permissions;
   the repository does not need broadly writable workflow-token defaults.
5. Commit and push the reviewed application, lockfile and CI/CD changes together to `main`.
   Watch **Actions → checks**. The `Deploy scheduler and gateway` job starts only after
   all three platform check jobs pass. Future passing pushes to `main` deploy automatically.

If you prefer CLI setup instead of those GitHub UI steps, authenticate `gh` as a repository
administrator and run `python3 ship/cloud/bootstrap.py` without `--manual-github`.

Defaults target `jimfangx/fletcherlake-scheduler`, GCP project `fletcherlake`, zone
`us-west2-a` and domain `fl.ucb.bar`; use `--help` for overrides. Bootstrap expects the
existing metadata-based SSH configuration, with OS Login disabled. It preserves the current
administrator keys and does not change OS Login. It is a separate operator setup command,
not another numbered initial provisioning step.

Bootstrap configures a Workload Identity Federation provider restricted to the numeric
repository/owner IDs, `main`, push events and `.github/workflows/ci.yml`. GitHub exchanges its
OIDC token for temporary GCP credentials; no Google service-account private key is created.
The deployment service account gets instance lookup permissions and IAP tunnel access to
only the scheduler's internal IP/port 22 and gateway's internal IP/port 2222.
See [Google's GitHub federation documentation](https://cloud.google.com/iam/docs/workload-identity-federation-with-deployment-pipelines).

Bootstrap adds narrowly targeted firewall rules for IAP's `35.235.240.0/20` source range
on those management ports. It creates a dedicated `fl-deploy` SSH account with sudo on the
two VMs and a separate Ed25519 deployment key. The private SSH key is stored as the GitHub
`production` environment secret `FL_DEPLOY_SSH_KEY`; the local copy is mode 0600 in the
operator state directory. SSH host keys are obtained through the existing authenticated
administrator connection and pinned in `FL_DEPLOY_KNOWN_HOSTS`. CI uses ordinary OpenSSH
over `gcloud compute start-iap-tunnel` without permissions to edit instance metadata.
See [IAP forwarding and its permission requirements](https://cloud.google.com/iap/docs/using-tcp-forwarding).

The following nonsecret production environment variables are installed automatically:
`FL_GCP_PROJECT`, `FL_GCP_ZONE`, `FL_DOMAIN`, `FL_SCHEDULER_VM`, `FL_GATEWAY_VM`,
`FL_WORKLOAD_IDENTITY_PROVIDER`, `FL_DEPLOY_SERVICE_ACCOUNT`, `FL_DEPLOY_KNOWN_HOSTS`.
An existing GitHub production environment retains its protection rules. For automatic
deployment without approval prompts, it must permit `main` and have no required reviewers.

Commit/push the CI/CD files together with the matching application and lockfile changes.
Bootstrap configures access; the workflow becomes active when its files reach `main`.

## Each deployment

1. Linux and both Mac architectures pass the existing checks. Linux builds/tests the web
   dashboard and packages the exact checked commit plus pinned lilikoi source and built
   dashboard into one artifact. Untracked files, Git metadata, environments and the Pico SDK
   are excluded. The same artifact is used for both cloud VMs.
2. A serialized production job skips superseded main commits. It authenticates through
   OIDC, uploads through pinned SSH over IAP, verifies the archive checksum, and installs
   the locked Python environment into a new `/opt/fl-releases/<sha>-<run>-<attempt>` on
   each VM while the current services are running.
3. After both releases prepare successfully, it stops the scheduler and gateway/transfer
   SSH server, backs up PostgreSQL and gateway SQLite metadata plus `/etc/fl`, and switches
   `/opt/fl` to the prepared releases. The first deployment moves the previous real
   `/opt/fl` directory into the releases directory before creating the symlink.
4. It runs `alembic upgrade head` using the scheduler's existing environment, starts/checks
   the gateway and transfer SSH server, then starts/checks the scheduler and public health
   endpoint. `/opt/fl/DEPLOYED_REVISION` on each host identifies the deployed commit.

Expect a short service/transfer interruption during steps 3–4; physical Mac jobs keep their
local execution and queues. Interrupted user submissions can resume with their saved receipt.
Deployments never run the initial scripts that create VMs, format disks or create databases.
Only one deployment runs at a time, and newer pushes do not cancel an in-progress deployment.
Operators can still cancel a job manually, so inspect both hosts after a manual cancellation.

## Failures and recovery

A preparation failure leaves the old services running. After stopping begins, any failure
triggers attempts to stop both services and leaves their release directories and backups
for inspection. The workflow does not automatically restore databases or downgrade schemas.
If a migration or health check fails, recover the two VMs together before accepting traffic.

Protected backups live at `/var/backups/fl-cloud/<sha>-<run>-<attempt>` on each VM. They include
`database.dump` on the scheduler, `gateway.db` on the gateway, `etc-fl/`, and a
`previous-release` pointer after activation. Gateway artifact bytes stay on their mounted
volume; metadata backup is not a copy of all artifacts. These local backups do not replace
off-host disaster recovery. Old releases and backups are retained; maintain disk retention
on both VMs and monitor free space before enabling frequent pushes.

For a compatible application rollback, stop both services, verify the previous release is
compatible with the current database schema, and restore the `/opt/fl` symlink on **both**
VMs to their recorded previous releases. Then start/health-check the gateway and scheduler.
If restoring PostgreSQL or gateway state is required, keep both services stopped and use a
reviewed database recovery procedure; avoid blindly replaying migrations or restoring a dump
over active writes. For ordinary code fixes, push a corrected commit to `main` and rerun CI.
