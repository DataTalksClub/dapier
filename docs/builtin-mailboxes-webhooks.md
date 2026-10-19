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
| `dev@dtcdev.click` | `/hooks/webhook/dev` | `dev-mailbox` | `dapier` |
| `tofu@dtcdev.click` | `/hooks/webhook/tofu` | `tofu-mailbox` | `tofu` |
| `orin@dtcdev.click` | `/hooks/webhook/orin` | `orin-mailbox` | `orin` |

Notify path: workflow `agent` step on the Hetzner dapier worker runs
`hub intake create` (direct Lambda → Hub HTTP is blocked by Cloudflare 403).

## Operator steps (repeatable)

```bash
# 1) Create / update webhook (JSON: name, kind=webhook, description)
dapier webhooks save webhook.json
dapier webhooks show dev    # copy URL + bearer; do not commit tokens

# 2) Save + publish mailbox workflow (email + webhook triggers)
dapier workflows save workflows/dev-mailbox.yaml
dapier workflows publish dev-mailbox.yaml
dapier emails list          # expect dev@ and tofu@

# 3) Test webhook
curl -sS -X POST https://dapier.dtcdev.click/hooks/webhook/dev \
  -H "authorization: Bearer $(dapier webhooks show dev | awk -FBearer {print })" \
  -H "content-type: application/json" \
  -d "{\"subject\":\"ping\",\"from\":\"you@example.com\",\"text\":\"hi\",\"message_id\":\"ping-1\"}"
dapier webhooks deliveries dev
dapier runs list --workflow dev-mailbox
```

Tokens: `dapier webhooks show`; never put them in git or shared reports.
