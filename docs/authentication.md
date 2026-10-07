# Scheduler authentication and authorization

Humans sign in through Google OAuth. The scheduler exchanges an authorization code using
PKCE, verifies the Google ID token through `google-auth` (signature, issuer, audience and
expiry), and additionally checks nonce, verified email, and the configured Workspace domain.
Only Google-authoritative Gmail/Workspace addresses are accepted. Google access tokens are
discarded; platform clients receive opaque scheduler credentials.

Google Groups is the access authority. Configure a required user group and optional operator
and administrator groups. Higher roles include lower-role capabilities. Each authenticated
request checks current membership through a cache with a maximum lifetime of 60 seconds.
Expired membership is never reused during Google Groups outages. PostgreSQL user records are
profiles and ownership metadata, not an allowlist. Administrators cannot grant membership by
editing this database; use Google Groups.

Choose `FL_GOOGLE_GROUPS_BACKEND=cloud_identity` and set `FL_GOOGLE_GROUPS_CREDENTIALS`
to a service-owned regular mode-0600 JSON key for a service account made an owner of each
configured Workspace group. This uses Cloud Identity's read-only Groups scope and **direct**
human membership, including owner/manager roles and member expiry. Nested-group membership
does not authorize access. The account does not impersonate an administrator or require
domain-wide delegation. Groups for Business must be enabled and organization policy must
allow that service-account owner. The scheduler rejects inaccessible groups, malformed
responses and redirects; absent direct members are denied.

The compatibility default `directory` retains Admin SDK Directory's delegated administrator
adapter and its existing `FL_GOOGLE_DIRECTORY_CREDENTIALS` / `FL_GOOGLE_DELEGATED_ADMIN`
configuration. See [the deployment guide](deployment-guide.md#3-google-login-and-groups-authorization)
for both configurations and [Google's authentication setup](https://docs.cloud.google.com/identity/docs/how-to/setup).

Browser login uses a one-use PostgreSQL state record bound to a separate secure, HttpOnly
cookie, plus PKCE and nonce. The callback accepts only two fixed return paths. Session cookies
have the `__Host-` prefix, Secure, HttpOnly, and SameSite=Lax. Mutating cookie-authenticated
requests must have the exact configured scheduler Origin. There is no permissive CORS policy.
All API responses prohibit caching. The HTTPS reverse proxy must strip incoming forwarded
headers, set its own headers, and avoid logging callback query strings or authorization headers.

Terminal login uses the scheduler's approval broker rather than Google's restricted television
device OAuth client. `POST /api/auth/terminal` returns a private polling secret, a displayed user
code, and the HTTPS verification URL. The user signs in with Google at that URL and explicitly
enters the code from their terminal. The terminal polls `/api/auth/terminal/poll` every five
seconds using the private secret. Challenges expire after ten minutes and are consumed once.
Knowing the displayed code cannot retrieve a token. Knowing the polling secret cannot approve
the code. Apply proxy rate limits to login/challenge/approval endpoints before public deployment.

Access tokens expire after 15 minutes; refresh tokens expire after seven days. Refresh rotates
both credentials in a row-locked transaction without extending the original refresh deadline.
Only SHA-256 hashes enter the session table. Logout revokes the entire session. Browser refresh
uses the HttpOnly refresh cookie and an Origin check; terminal refresh supplies its credential
in a JSON body. Neither tokens nor Google client/service-account secrets belong in inventory
or job YAML. OAuth state includes a short-lived PKCE verifier: protect PostgreSQL and backups
as secret-bearing infrastructure. Periodic maintenance deletes expired authentication state.

Job reads, events, collateral metadata, cancellation, and deletion require the owner or an
operator/administrator. Ordinary users see only their jobs in the global job list. Cluster
inventory omits agent credential hashes and connection identifiers; active job IDs belonging
to other users are hidden. Operators may drain clusters. Administrators may inspect user
profiles. Enrollment, remote lifecycle operations, and artifact download credentials are
separate integration work recorded in the requirement ledger.

User identity is tied to Google's immutable subject. An email change or an email reused by
another Google subject is rejected for administrator review, because existing job ownership
uses email addresses. Membership is still required before any new session can be issued.

Tests inject Google providers but use real PostgreSQL and HTTP requests. The production
Google verifier is also exercised with locally signed RSA tokens and an injected public-key
response, including bad signatures, audiences, issuers, and expiration. They prove one-use
state binding, code approval, membership removal, cache expiration, provider outages, role
boundaries, ownership checks, token rotation/expiry/revocation, and secret-free inventory.
Cloud Identity tests also sign service-account assertions without an impersonated subject,
intercept the membership API, and verify direct role checks, expiry/removal, failure handling,
credential-file protection and real PostgreSQL session issuance. They do not claim a live
Google Workspace integration: deployer-provided credentials and authorized group access
are required for that acceptance check.
