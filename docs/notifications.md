# Scheduler notifications

The Linux scheduler consumes durable events and delivers through Mailgun, Slack incoming
webhooks, and Google Chat incoming webhooks. Mac executors record execution events; they contain
no provider credentials or delivery logic. Sending a notification never changes a hardware
outcome, cancels a job, or transfers artifact data.

Notifications are disabled when `FL_NOTIFICATION_CONFIG_FILE` is absent. Configure them by
copying [the example](../examples/notifications.json) to a protected service-owned file, replacing
all placeholders, and setting that variable to its absolute path in the scheduler environment.
The file must be regular, owned by the scheduler's UID, at most 64 KiB, and mode 0600 or stricter.
Symlinks and public permissions are rejected. For a conventional Linux deployment:

```sh
sudo install -o fl-scheduler -g fl-scheduler -m 0600 examples/notifications.json /etc/fl/notifications.json
sudoedit /etc/fl/notifications.json
```

Set `FL_NOTIFICATION_CONFIG_FILE=/etc/fl/notifications.json` in the protected environment file
and restart after running Alembic upgrade head. The service requires revision
`0010_notification_delivery`. Stop old scheduler instances before this migration and restart
with the matching binary; agents continue execution independently. Actual account credentials and destination approval belong to
the deployment operator. The test suite intercepts HTTP and sends no external messages.

## Destinations and content

Mailgun uses its US or EU HTTPS Messages endpoint, HTTP Basic authentication, multipart fields,
and a stable custom Message-ID for correlation. Configure a verified sending domain and sender
address. Operator addresses receive eligible job and cluster notices. With `include_owner: true`,
job owners also receive mail if they have a registered Google identity and a valid basic ASCII
email address. Local Unix usernames are not email destinations. Duplicate owner/operator addresses
receive one notice. The adapter follows the [Mailgun Messages API](https://documentation.mailgun.com/docs/mailgun/api-reference/send/mailgun/messages/post-v3--domain-name--messages).

Slack posts JSON to one configured incoming webhook, with a plain-text block and disabled link
unfurling. Its webhook determines the destination channel; the notification cannot override it.
Both commercial Slack and GovSlack webhook hosts are supported. Treat the entire webhook URL
as a credential. See [Slack's incoming webhook contract](https://docs.slack.dev/messaging/sending-messages-using-incoming-webhooks/).

Google Chat posts text to the configured space's webhook. The URL must have `key` and `token`
query parameters. The adapter adds a stable notification UUID as `requestId` on every retry.
The [Messages API](https://developers.google.com/workspace/chat/api/reference/rest/v1/spaces.messages/create)
documents request-ID idempotency; see also the [webhook setup guide](https://developers.google.com/workspace/chat/quickstart/webhooks).

Each notice contains a fixed event label, event time, job or cluster UUID, authenticated metadata
link, notification UUID, and an expiry when available. It contains no UART bytes, JobConfig,
artifact data, arbitrary error strings, or provider secrets. Slack/Chat content omits the owner
email. The configured channels and operator addresses form the deployment's trusted audience.

Remove a provider from the configuration to stop that destination. A remaining worker marks
its pending rows SKIPPED rather than sending them elsewhere. Removing the environment variable
pauses all notification processing; existing rows remain durable. Changing operator addresses
affects new projection; existing email destinations remain frozen. Secret rotation for the same
logical provider destination preserves its queue identity. Moving to another channel/space or
Mailgun sending identity creates a different route, and existing intent is not silently rerouted.

## Events and warning policy

Consumers handle job success, failure, timeout, cancellation, interruption/loss, possible hang,
cluster offline/online transitions, artifact expiry warnings, and completed artifact deletion.
Other events, including JOB_LOG and administrator retry audit events, do not generate notices.

The Mac emits one JOB_HANG_WARNING if execution is still active after 90% of its configured
deadline budget. This is a possible-hang warning, not a claim that hardware has hung: a long
legitimate programming or run phase can reach the same budget. UART silence does not trigger
it. Success/cancellation reaps the warning task, and the existing execution deadline decides
the authoritative timeout outcome.

Scheduler health maintenance emits CLUSTER_OFFLINE after heartbeat expiry; an authoritative
new snapshot emits CLUSTER_ONLINE. Concurrent sweeps share scheduling serialization, so one
health transition creates one event. Connectivity changes leave jobs and assignments intact.

Retention maintenance warns within 24 hours of the completion-based expiry, once per job/expiry.
Its query handles at most 100 eligible artifact records per sweep, excludes already warned jobs,
and advances to later batches. Already deleted, manually deleting, active, and expired collateral
are excluded. A zero-retention job can expire immediately and has no advance warning window.
ARTIFACT_DELETED follows the Mac's durable deletion effect and can replay after reconnection.

## Projection, retries and restart

Route activation is persisted. By default, a new route consumes events ingested after its first
activation, avoiding an unsolicited historical backlog. Restarts keep the original activation
time and catch up while the service was offline. Optional `backfill_since` is an aware ISO timestamp
that explicitly extends the route to earlier scheduler ingestion times. It can replay historical
notices for newly configured destinations. Source Mac event timestamps remain in the message;
the scheduler's ingestion timestamp controls subscription boundaries despite Mac clock skew.

Each route/event projection and its delivery rows commit together. Unique identities deduplicate
concurrent projectors. Scans query unprojected events rather than advancing a highest-ID cursor:
PostgreSQL sequence allocation does not imply transaction commit order. A late lower-ID commit
therefore remains eligible. A route with no applicable email recipients is still marked projected.

Delivery claims use PostgreSQL row locks and a 60-second lease with a random token. Worker
completion must match the current unexpired token. Concurrent instances pace each logical
provider route at one request per second; a 429 Retry-After also delays other rows for that route.
Slack's [rate limit contract](https://docs.slack.dev/apis/web-api/rate-limits/) and Chat's
[webhook limits](https://developers.google.com/workspace/chat/quickstart/webhooks) inform this pacing.

Requests reject redirects, use a 15-second HTTP timeout and a 20-second overall send budget,
and require provider-specific success acknowledgement. Transport failures, invalid positive
acknowledgements, HTTP 408/425/429 and server errors retry with persisted exponential backoff
from two seconds through five minutes. Retry-After seconds or HTTP dates can extend the delay
up to one day. Other HTTP errors are permanent failures. Attempt counts include claimed attempts
lost to a process crash. The default budget is eight attempts; `max_attempts` accepts 1–20.

Cancellation reaps database work and outbound tasks before the scheduler closes its client/pool.
An interrupted send retains its lease for recovery. If a provider accepted a request before the
response was lost, a later attempt can duplicate an external message. Slack and Mailgun delivery
are at least once; a stable Message-ID is correlation, not a Mailgun deduplication guarantee.
Google Chat retries use the documented request-ID contract with identical content/credentials.
There is no claim of atomicity between an external provider and PostgreSQL.

## Inspect and retry

Administrators can query `GET /api/admin/notifications?limit=100&offset=0` for state, cumulative
attempts, safe error code, HTTP status, next attempt and completion time. Credentials, webhook
URLs, destination addresses and raw provider response bodies are omitted. Ordinary users and
operators cannot inspect or retry this global queue.

After fixing configuration, `POST /api/admin/notifications/<UUID>/retry` reopens a FAILED or
SKIPPED routed row with a fresh budget. It preserves the notification UUID, original content,
destination and cumulative attempts, and appends an audit event. Repeating the request after
acceptance or successful delivery is a no-op. An old pre-consumer scaffold row lacks a destination
and cannot be reconstructed; migration marks it SKIPPED with LEGACY_UNROUTED.

Linux acceptance covers provider envelopes and acknowledgement validation, redirect rejection,
protected settings, concurrent projection, late commits, clock skew, durable activation/backfill,
lease fencing, shared rate limits, lost responses, worker cancellation/replacement, removed
destinations, retry budgets and administrator authorization, plus real Mac-simulator event
replication and bounded retention batches. Live provider acceptance and native Mac hardware
warning behavior still require deployment credentials and hardware.
