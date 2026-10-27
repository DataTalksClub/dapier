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
| `dev@dtcdev.click` | `/hooks/webhook/dev` | `dev-mailbox` | Developer bot webhook |
| `tofu@dtcdev.click` | `/hooks/webhook/tofu` | `tofu-mailbox` | Tofu Grok bot webhook |
| `orin@dtcdev.click` | `/hooks/webhook/orin` | `orin-mailbox` | Orin Grok bot webhook |

Notify path: each mailbox workflow (`dev-mailbox`, `tofu-mailbox`,
`orin-mailbox`) forwards directly to its own Grok bot webhook via HTTP POST
without running an agent and without sending any email. Each workflow reads
its own storage keys `webhook_url` / `webhook_token` (per-workflow storage,
same shape for all three), fails fast with a clear error naming the missing
keys when either is unset (no POST to an empty URL), and POSTs JSON with
`from`, `to`, `subject`, `snippet` (first 200 chars of the plain-text body),
`text` (full body), `date`, `message_id` (dedupe), and attachments metadata
(filename, content_type, size, plus checksum/content_id when present).

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
dapier storage set tofu-mailbox webhook_url "<tofu-bot-webhook-url>"
dapier storage set tofu-mailbox webhook_token "<tofu-bot-bearer-token>"
dapier storage set orin-mailbox webhook_url "<orin-bot-webhook-url>"
dapier storage set orin-mailbox webhook_token "<orin-bot-bearer-token>"

# 3) Verify an existing mail delivery through its workflow run.
dapier runs list --workflow dev-mailbox
# For synthetic tests use workflows test (dry-run); see docs/testing-inbound-email.md.
```

Tokens: `dapier webhooks show`; never put them in git or shared reports.

Run history masks storage values whose key contains `token`, `secret`,
`password`, `authorization`, or `api_key` (case-insensitive). For other
`storage_get` keys, set `secret: true` on the step to hide its recorded value.
Later steps still receive the real value through `{steps.<id>.output.value}`.
Recorded HTTP credentials (including `auth_token` and authorization headers)
are masked before storage and truncation. Console, CLI, export, and content
search share the same redaction; older matching records are also masked on
read, without rewriting or deleting historical data. A retry or replay that
reuses a masked storage read fetches its current value from workflow storage.
