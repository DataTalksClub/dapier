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
its own sender-policy row inherits the default allowlist (Alexey's two
addresses). Set an explicit **allowlist** for each assistant mailbox and add
its expected alert senders; do not switch bot mailboxes to `anyone`:

```bash
dapier emails from allowlist dev
dapier emails from add noreply@datatalks.club --route dev
dapier emails from add no-reply@dtcdev.click --route dev
dapier emails from list --route dev
```

A new allowlist copies the default senders. The dev policy above was already
applied on production on 2026-10-24; these commands document setup, not a
request to reapply it.

Sender-policy refusals at an enabled workflow mailbox log an ERROR naming
the address, sender, and owning workflows, and emit `EmailSenderPolicyRefused`
in `Dapier/Triggers`. Atomic claims in the cursors table dedupe each
address/sender pair for 24 hours, including concurrent workers and queue
redeliveries. `EmailSenderPolicyRefusalAlarm` uses the existing operator alarm
topic and stays active while any hour in the last day has a refusal. The
inbox still shows `refused: sender not allowed`; no workflow action runs.
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

## Live examples (2026-10-19)

| Address | Webhook | Workflow | Hub project |
| --- | --- | --- | --- |
| `dev@dtcdev.click` | `/hooks/webhook/dev` | `dev-mailbox` | Developer bot webhook |
| `tofu@dtcdev.click` | `/hooks/webhook/tofu` | `tofu-mailbox` | `tofu` |
| `orin@dtcdev.click` | `/hooks/webhook/orin` | `orin-mailbox` | `orin` |

Notify path: workflow `agent` step on the Hetzner dapier worker runs
`hub intake create` for tofu@ and orin@. For dev@, `dev-mailbox` forwards
directly to the Developer bot webhook via HTTP POST without running an agent.

For a matching per-assistant bot setup, keep each assistant's own mailbox
and allowlist, and provision its own destination webhook URL and token in
that mailbox workflow's storage (`webhook_url` / `webhook_token`). The
`/hooks/webhook/tofu` trigger is an inbound Dapier hook; it does not itself
connect to a Tofu Grok Bot. Tofu currently uses the agent-to-Hub intake path.
Do not replace that path until Tofu's destination webhook values are supplied.

## Operator steps (repeatable)

```bash
# 1) Create / update webhook (JSON: name, kind=webhook, description)
dapier webhooks save webhook.json
dapier webhooks show dev    # copy URL + bearer; do not commit tokens

# 2) Save + publish mailbox workflow (email + webhook triggers)
dapier workflows save workflows/dev-mailbox.yaml
dapier workflows publish dev-mailbox.yaml
dapier emails list          # expect dev@ and tofu@

# 2b) Provision the Developer bot webhook config dev-mailbox reads.
# The workflow's storage_get steps return "" for keys that were never set,
# and an empty webhook URL fails every dev@ email through five queue
# redeliveries into the dead-letter queue (EventDeadLetterAlarm). Set once,
# re-set after a storage TTL expiry or when rotating the token:
dapier storage set --workflow dev-mailbox --key webhook_url --value "https://api2.cursor.sh/automations/webhook/<target-id>"
dapier storage set --workflow dev-mailbox --key webhook_token --value "<developer-bot-bearer-token>"
dapier storage get --workflow dev-mailbox --key webhook_url   # expect found

# 3) Test webhook
curl -sS -X POST https://dapier.dtcdev.click/hooks/webhook/dev \
  -H "authorization: Bearer $(dapier webhooks show dev | awk -FBearer {print })" \
  -H "content-type: application/json" \
  -d "{\"subject\":\"ping\",\"from\":\"you@example.com\",\"text\":\"hi\",\"message_id\":\"ping-1\"}"
dapier webhooks deliveries dev
dapier runs list --workflow dev-mailbox
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
