# Built-in per-bot mailboxes + webhooks (dev / tofu / orin)

Alexey (t526u–t527u): create addresses and connect webhooks through **dapier
built-ins**, not terraform SES / shared aws-infra.

## Built-in create email

Email addresses are workflow triggers. Publishing a workflow with

```yaml
trigger:
  connector: email
  event: message.received
  filters:
    route: { equals: <local-part> }
```

claims `<local-part>@dtcdev.click` in the Emails inventory
(`dapier emails list|show`). There is no separate mailbox store.

### Per-address sender policy is required for bot alerts

Claiming a mailbox does not authorize its alert senders. An address without
its own sender-policy row inherits the default allowlist. Set an explicit
**allowlist** for each bot mailbox and keep the five alert senders on each
route; do not switch bot mailboxes to `anyone`, and do not remove old
senders without Alexey's explicit OK:

```bash
dapier emails from allowlist dev
dapier emails from add alerts@dtcdev.click --route dev
dapier emails from add noreply@datatalks.club --route dev
dapier emails from add no-reply@dtcdev.click --route dev
dapier emails from list --route dev
# repeat for --route tofu and --route orin
```

Expected per-route allowlist (each of dev / tofu / orin):

- alexey.s.grigoriev@gmail.com
- alexey@datatalks.club
- alerts@dtcdev.click
- noreply@datatalks.club
- no-reply@dtcdev.click

A new allowlist copies the default senders. Add all five required addresses
explicitly when provisioning each bot mailbox. The bot's own outgoing/notification senders are never added to
these allowlists, and the mailbox workflows send no email, so a bot reply
cannot re-trigger its own mailbox.

The `dev` oncall mailbox also allows `no-reply@sns.amazonaws.com` for SNS
subscription confirmations and alerts. Add it only to `dev`, preserving the
five senders above and the default policy:

```bash
dapier emails from add no-reply@sns.amazonaws.com --route dev
dapier emails from list --route dev
```

On 2026-11-10, this missing sender blocked confirmations for the SNS topics
`operator-alerts` and `course-management-operator-alerts` in AWS account
`387546586013`. Both messages remain in the inbox with outcome `refused`;
neither contained an alarm payload. Allowing the sender accepts future mail;
it does not confirm subscriptions or replay previous deliveries. Check the
subscriptions separately before relying on their alert delivery.

The `dev` oncall mailbox also allows `noreply@aishippinglabs.com`, the AI
Shipping Labs app's SES sender for operational failure alerts (campaign queue
and stats-refresh failures, subjects `AISHL campaign ... failed: <id>`). Add it
only to `dev`:

```bash
dapier emails from add noreply@aishippinglabs.com --route dev
dapier emails from list --route dev
```

On 2026-11-13, this missing sender refused two AISHL alerts for campaign 18
(inbox events `52445316...` "campaign stats refresh failed" and `59c09af2...`
"campaign queue failed": Relay preflight recipient count mismatch). Both stay
in the inbox with outcome `refused`. Allowing the sender accepts future mail;
replay the refused events (`dapier runs events replay <id>`) to deliver them
to oncall.

The `dev` mailbox also allows `courses@datatalks.club`, the DataTalks.Club
academy/courses app's SES sender. Its signup confirmation mails ("Confirm Your
Email for ...", "Confirm your registration: ...") go to plus-addressed test
aliases such as `dev+waitlist-ai-20261115@dtcdev.click` during signup-flow
checks. Add it only to `dev`:

```bash
dapier emails from add courses@datatalks.club --route dev
```

On 2026-11-14/15, six such test confirmations were refused (inbox events
`db4bc01a...`, `890b096a...`, `83c8d0ae...`, `bd662902...`, `cf3d169d...`,
`48ca8f15...`) and fired `EmailSenderPolicyRefusalAlarm`. They were test
sign-ups whose single-use confirm links had already served their purpose, so
they were not replayed to oncall.

The `dev` mailbox also allows `content@aishippinglabs.com`, the AI Shipping
Labs campaign sender (Relay client `aisl-website`). Campaign test sends
(`asl campaigns test-send <id> --email dev+<tag>@dtcdev.click`) land here so an
agent can check that the test arrived and renders before a real send. Add it
only to `dev`:

```bash
dapier emails from add content@aishippinglabs.com --route dev
```

On 2026-11-15, three campaign 25 test sends (Relay messages 42308, 42311,
42317, SES-delivered to the dtcdev.click MX) were refused with
`sender_not_in_allowlist` (inbox events `d1b71443...`, `b439e018...`,
`ac82c892...`). Refused tests are not replayed; send a new test instead.

Sender-policy refusals at an enabled workflow mailbox log an ERROR naming
the address, sender, owning workflows, policy rule, and event id, and emit `EmailSenderPolicyRefused`
in `Dapier/Triggers`. Atomic claims in the cursors table dedupe each
address/sender pair for 24 hours, including concurrent workers and queue
redeliveries. `EmailSenderPolicyRefusalAlarm` uses the existing operator alarm
topic and stays active while any hour in the last day has a refusal. The
inbox still shows `refused: sender not allowed`; no workflow action runs.
Alarm emails include recipient, sender, rule (`sender_not_in_allowlist` or
`missing_sender`), owning workflow, and inbox/event id from refusals in the
24-hour evaluation window. A refused email has no run id because it never
starts a workflow; the alert states this explicitly. The notifier reads only
envelope metadata from the existing inbox and lists at most 20 recent
refusals, without message subjects, bodies, attachments, or arbitrary payloads.
Disabled, auto-paused, and unclaimed addresses do not emit the signal.

The refusal handler sends no email or forwards mail. Refused Dapier operator
notices (alarm, failure, auto-pause, and error-digest subjects from Dapier's
configured sender) log the ERROR but emit no metric, so a refusal notice
cannot recursively trigger another notice. Ordinary alerts from that same
sender still signal. Webhook POST errors remain failed runs, and missing
webhook storage keys still fail fast. Alarm recipients are unchanged.

## Built-in connect webhook

```bash
dapier webhooks save webhook.json   # or PUT /api/agent/hook-triggers
dapier webhooks show <name>         # URL + bearer token (operator only)
dapier webhooks test <name>
```

Reserves `https://dapier.dtcdev.click/hooks/webhook/<name>` live immediately
(no deploy). Pair with a workflow trigger:

```yaml
trigger:
  connector: webhook
  event: request.received
  filters:
    hook: { equals: <name> }
```

## Live examples

| Address | Webhook | Workflow | Destination |
| --- | --- | --- | --- |
| `dev@dtcdev.click` | `/hooks/webhook/dev` | `dev-mailbox` | Developer bot webhook + direct oncall ingest (or Dev sticky Hub session when GrokBot usage < 1%) |
| `tofu@dtcdev.click` | `/hooks/webhook/tofu` | `tofu-mailbox` | Tofu Grok bot webhook (or Tofu sticky Hub session when GrokBot usage < 1%) |
| `orin@dtcdev.click` | `/hooks/webhook/orin` | `orin-mailbox` | Orin Grok bot webhook (or Orin sticky Hub session when GrokBot usage < 1%) |

Notify path: `dev-mailbox` posts every dev@ alert to both the existing
Developer/Grok webhook and the direct oncall ingest at
`https://hub.dtcdev.click/webhooks/oncall`. The direct receiver is Hub2, a
Node server on the GCP VM with a durable SQLite queue. The Hub host polls that
queue and invokes `~/bin/oncall-send` followed by `~/bin/orch-send`; those
helpers save to the oncall inbox before waking live sessions. The direct
oncall ingest always runs (it is independent of the GrokBot quota route),
then the workflow fails if a routed leg or the ingest did not complete so
Dapier retries the event. A stable delivery ID makes replay of the direct
ingest idempotent. Developer remains on the path until its hop is
intentionally removed after the direct path is proven.

`dev-mailbox` reads `webhook_url` / `webhook_token` for the existing
Developer/Grok endpoint and `oncall_webhook_url` / `oncall_webhook_token` for
Hub2. Keep all values in workflow storage. The tofu and orin workflows still
read only their own `webhook_url` / `webhook_token` keys and post to their own
Grok webhooks. Each webhook payload includes `from`, `to`, `subject`,
`snippet` (first 200 chars of the plain-text body), `text` (full body), `date`,
`message_id`, and attachments metadata (filename, content_type, size, plus
checksum/content_id when present).

## GrokBot <1% quota fallback (sticky Hub session)

Intake f28baca0: GrokBot (the Grok-powered bots behind orin / tofu / dev)
regularly runs out of quota, so a mailbox mail that only reaches its bot
webhook is lost. Each mailbox workflow therefore checks Hub's GrokBot usage
export and, when the bot is below threshold, notifies the role's sticky
interactive Hub session on `DESKTOP-FP4OP26` with "Hey, check your emails."
instead of posting to the bot webhook.

Hub-side contract (agent-hub intake 408b585e, deployed on the same Hub):

- `GET /api/quota/grokbot` with a Hub bearer token returns
  `{"account": "grokbot", "provider": "grok", "remaining_percent": <number>,
  "threshold_percent": 1, "below_threshold": <bool>, "fresh": <bool>,
  "state": "fresh|stale|exhausted|unknown", "observed_at": ..., "reset_at": ...}`.
  Hub sets `below_threshold` only on fresh evidence, never on stale/unknown.
- `POST /api/messages` with `{"recipient": "agent:<role>", "body": "Hey,
  check your emails.", "key": "dapier-<role>-<delivery sha>"}` enqueues the
  message for the named agent; Hub forwards it into the live sticky session
  on Desktop (durable mailbox delivery when none is live) and dedupes on
  `key`, so workflow retries cannot double-notify.

Workflow behavior (all three mailboxes, `route` condition):

- `grok-fallback-armed` runs the usage check only when all six hub storage
  keys below are provisioned; otherwise no Hub call is made.
- `decide-grok-route` diverts only when the check completed with
  `below_threshold: true` **and** `fresh: true`. Fail-safe by design: a Hub
  outage (skipped/failed check), stale evidence, or half-provisioned storage
  keeps the historical webhook delivery — a quota fallback must never
  divert mailbox mail into the void.
- When diverted, the webhook leg does not run; when not diverted, the
  webhook leg runs exactly as before. `dev-mailbox` additionally always runs
  its direct oncall ingest regardless of the route.

Token rotation: Hub bearers are human device credentials whose access tokens
expire hourly. Each mailbox stores `hub_refresh_token` alongside
`hub_api_token`; when the usage check answers HTTP 401, the same run trades
the refresh token for a fresh pair (`POST /api/token/refresh`, grant_type
`refresh_token`) and overwrites both keys with `storage_set`. The expired
run itself still delivers via the webhook (fail-safe); the next email uses
the fresh bearer, so the fallback self-heals within one email of an expiry.
Hub grants refresh-token replay grace, which covers the three mailboxes
rotating copies of the same family concurrently.

Storage keys per mailbox workflow (all optional; the fallback arms only when
all are set — provision them to enable the fallback):

```bash
dapier storage set <wf> hub_usage_url "https://hub.dtcdev.click/api/quota/grokbot"
dapier storage set <wf> hub_messages_url "https://hub.dtcdev.click/api/messages"
dapier storage set <wf> hub_api_token "<Hub-bearer-token>"
dapier storage set <wf> hub_refresh_token "<Hub-refresh-token>"
dapier storage set <wf> hub_refresh_url "https://hub.dtcdev.click/api/token/refresh"
dapier storage set <wf> hub_recipient "agent:orin"   # agent:tofu / agent:dev
```

The bearer is the human device login (`client_kind: human`,
`~/.config/agent-hub/cli.json` after `hub auth login --label dapier`): Hub
refuses `kind=service` actors on everything except `/api/model-policy`, and
the human grant carries the `operator:admin` scope both routes need. Access
tokens rotate hourly; the workflow rotates them itself from the stored
refresh token (see above), so manual re-provisioning is only needed when
Hub reports `invalid_grant` (family revoked — log in again and re-store
both tokens).

The delivery idempotency key (`dapier-<role>-<sha256 of message_id>`) is
stable across queue redeliveries of the same email, so a replay dedupes at
Hub instead of double-pinging the role's session.

Loop safety: the mailbox workflows contain no email-send step, and the bot's
own outgoing/notification senders stay off the sender allowlists, so a bot
reply cannot re-trigger its own mailbox.

## Operator steps (repeatable)

```bash
# 1) Create / update webhook (JSON: name, kind=webhook, description)
dapier webhooks save webhook.json
dapier webhooks show dev    # copy URL + bearer; do not commit tokens

# 2) Save + publish mailbox workflows (email + webhook triggers)
dapier workflows save workflows/dev-mailbox.yaml
dapier workflows publish dev-mailbox.yaml
dapier workflows save workflows/tofu-mailbox.yaml
dapier workflows publish tofu-mailbox.yaml
dapier workflows save workflows/orin-mailbox.yaml
dapier workflows publish orin-mailbox.yaml
dapier emails list          # expect dev@, tofu@, orin@

# 2b) Provision each bot's webhook config in its own mailbox workflow storage.
# The console designer "Stored data" panel edits the same per-workflow keys
# without SSH (open the workflow, Stored data, set webhook_url / webhook_token),
# and `dapier storage` below reaches the same API. The workflow's storage_get
# steps return "" for keys that were never set, and an empty webhook URL would
# fail every mail through five queue redeliveries into the dead-letter queue
# (EventDeadLetterAlarm). Set once per mailbox, re-set after a storage TTL
# expiry or when rotating the token. The token is accepted with or without a
# leading "Bearer " prefix; values are never printed in logs or reports.
dapier storage set dev-mailbox webhook_url "https://api2.cursor.sh/automations/webhook/<target-id>"
dapier storage set dev-mailbox webhook_token "<developer-bot-bearer-token>"
# Hub2 setup creates one-time ingest and poll tokens through
# POST https://hub.dtcdev.click/api/oncall-alerts/config. Store the ingest
# token only in Dapier; store the poll token in the Hub host's private
# oncall-alerts.json config. Never put either token in Git or a report.
dapier storage set dev-mailbox oncall_webhook_url "https://hub.dtcdev.click/webhooks/oncall"
dapier storage set dev-mailbox oncall_webhook_token "<Hub2-ingest-token>"
dapier storage set tofu-mailbox webhook_url "<tofu-bot-webhook-url>"
dapier storage set tofu-mailbox webhook_token "<tofu-bot-bearer-token>"
dapier storage set orin-mailbox webhook_url "<orin-bot-webhook-url>"
dapier storage set orin-mailbox webhook_token "<orin-bot-bearer-token>"
# 2c) Arm the GrokBot <1% quota fallback per mailbox (see the section above).
# Without these keys the fallback stays disarmed and mail goes to the bot
# webhook exactly as before. hub_api_token is masked in run history (key
# contains "token"); never commit it.
dapier storage set dev-mailbox hub_usage_url "https://hub.dtcdev.click/api/quota/grokbot"
dapier storage set dev-mailbox hub_messages_url "https://hub.dtcdev.click/api/messages"
dapier storage set dev-mailbox hub_api_token "<Hub-bearer-token>"
dapier storage set dev-mailbox hub_recipient "agent:dev"
# repeat for tofu-mailbox (agent:tofu) and orin-mailbox (agent:orin)

# 3) Verify an existing mail delivery through its workflow run.
dapier runs list --workflow dev-mailbox
# For synthetic tests use workflows test (dry-run); see docs/testing-inbound-email.md.
```

Tokens: `dapier webhooks show`; never put them in git or shared reports.

Run history masks storage values whose key contains `token`, `secret`,
`password`, `authorization`, or `api_key` (case-insensitive). For other
`storage_get` keys, set `secret: true` on the step to hide its recorded value.

## Hub task closed-loop webhook (`hub-task-closed-loop`)

`workflows/hub-task-closed-loop.yaml` listens on webhook hook
`hub-task-status` (`https://dapier.dtcdev.click/hooks/webhook/hub-task-status`)
for Hub task lifecycle events (started, finished/completed, failed/blocked) and
forwards a formatted status message to Dev — using Dev's sticky Hub session
(`agent:dev` via `POST /api/messages`) when GrokBot remaining usage is `< 1%`
on fresh evidence, or Dev's bot webhook (`webhook_url`) otherwise. It uses the
same per-workflow storage keys (`webhook_url`, `webhook_token`, `hub_*`) as
`dev-mailbox`.
Later steps still receive the real value through `{steps.<id>.output.value}`.
Recorded HTTP credentials (including `auth_token` and authorization headers)
are masked before storage and truncation. Console, CLI, export, and content
search share the same redaction; older matching records are also masked on
read, without rewriting or deleting historical data. A retry or replay that
reuses a masked storage read fetches its current value from workflow storage.
