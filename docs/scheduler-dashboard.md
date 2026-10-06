# Scheduler dashboard

The React/TypeScript application in `services/dashboard` uses the public scheduler origin.
It displays PostgreSQL projections of agent snapshots. The Mac daemon remains authoritative
for board execution and durable queues; browser commands wait for reported hardware state.

## Build and serve

From the repository root:

```sh
pixi install --locked -e web
pixi run -e web web-install
pixi run -e web web-check
pixi run -e web web-test
pixi run -e web web-build
export FL_DASHBOARD_DIR="$PWD/services/dashboard/dist"
pixi run fl-scheduler
```

The separate `web` environment supplies Node 24 for building. npm's committed lock pins React,
TypeScript, Vite and testing dependencies. Mac agent provisioning continues to use the default
Python environment. Production serves static build files without Node. Build before deployment;
startup rejects a missing index or assets directory. Use the existing public HTTPS nginx listener,
`FL_PUBLIC_ORIGIN`, and Google callback configuration in [scheduler operations](scheduler-operations.md).
The dashboard is optional when `FL_DASHBOARD_DIR` is unset.

## Views and permissions

| Route | Contents |
| --- | --- |
| `/clusters` | Health, state, configured board inventory, running and queued counts; search |
| `/clusters/:id` | Reported boards, host/tool inventory, CPU/RAM/disk, pending drain |
| `/boards/:id` | Hardware configuration, device mappings, queue count and permitted active job |
| `/jobs` | Owner-filtered, paginated jobs with state and assignment |
| `/jobs/:id` | Execution metadata, requested cancellation, retained artifact metadata and paged events |
| `/admin/clusters` | Issue/revoke enrollment tickets and inspect enrollment history |
| `/admin/users` | Known signed-in users; Google Groups remains the authorization source |

Owners can cancel their jobs and delete terminal collateral. Operators/admins can manage other
jobs and request cluster drain. Administrative pages require the administrator role. The backend
enforces every permission independently of navigation visibility. Destructive buttons ask for
confirmation. A successful command means it was accepted, and never changes the displayed
execution state optimistically. Kasa configuration appears as an unavailable force-off placeholder.
Submission and large retained-file downloads use [fl-client](remote-client.md), preserving the
gateway transfer path.

## Authentication and refresh

Google sign-in establishes Secure, HttpOnly cookies; JavaScript does not read or persist human
credentials. Fetches use relative `/api/` paths and same-origin cookies. Only rejected 401
authorization permits a shared session rotation and replay; network/5xx mutations are never
automatically repeated. Browser mutations retain the scheduler's Origin check. Sign-out removes
the authenticated tree, revokes the server session and clears cookies.

Resource reads poll every five seconds while visible (session membership every thirty seconds),
serialize pending requests, time out after thirty seconds and cancel on navigation. Refresh
rotation is bounded to fifteen seconds. Transient failures retain explicitly marked stale data;
401/403/404 clear private resource metadata. Late route responses cannot replace current data.
Running/queued counts come from the last accepted agent snapshot, expose no queue job IDs,
and remain unknown before a snapshot. Health and snapshot timestamps distinguish old data.

One-time enrollment tokens appear masked, can be revealed/copied, and remain only in component
memory. Leaving the page, session loss, refresh failure, dismissal or expiry removes the token.
Neither localStorage nor sessionStorage holds credentials. Shell routes and assets have explicit
paths; unknown API paths never receive a dashboard fallback. Shell CSP allows same-origin
scripts/styles/fetches and prohibits embedding; responses are no-store with no-referrer/nosniff.

## Verification

Focused Vitest tests cover shared refresh, failed mutation replay, sign-out, stale reads, route
races and request serialization. Real browser acceptance uses Chromium, a disposable PostgreSQL
database, the production build, and two local HTTPS origins. An injected Google provider redirects
through the actual scheduler login/callback and cookie validation; no live Google/Headscale call
is made. Tests cover all routes, reload, owner/admin boundaries, cancellation without invented
hardware completion, enrollment issue/revoke and token lifetime, sign-out, desktop/mobile layouts,
and request-origin confinement. Protected fixture credentials remain in a mode-0600 temporary file.

```sh
# From services/dashboard; builds/checks above run from the repository root.
PLAYWRIGHT_BROWSERS_PATH=/tmp/fl-browsers pixi run -e web npx playwright install chromium
# From the repository root; local socket access is required.
FL_TEST_DASHBOARD_BROWSER=/tmp/fl-browsers pixi run pytest -q \
  tests/integration/test_scheduler_dashboard.py tests/integration/test_dashboard_queries.py
```

Without `FL_TEST_DASHBOARD_BROWSER`, pytest explicitly skips browser acceptance. Linux CI builds
the client, installs Chromium and enables this check; macOS Python checks keep their default
environment. Screenshots are local acceptance artifacts under `/tmp`, and test output directories
are ignored. Live Workspace configuration and actual deployment remain separate acceptance gates.
