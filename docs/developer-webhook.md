# Developer webhook and per-bot @dtcdev.click mailboxes

## Webhook URL

```text
POST https://dapier.dtcdev.click/hooks/developer
```

Auth (either):

- `X-Dapier-Signature: sha256=<hex>` where hex is HMAC-SHA256(secret, raw body)
- `Authorization: Bearer <same secret>`

Secret source (env / Secrets Manager, never git):

- `DEVELOPER_WEBHOOK_SECRET` (literal) or `DEVELOPER_WEBHOOK_SECRET_ID`
- Hub call uses `HUB_INTAKE_TOKEN_SECRET_ID` (bearer token or JSON `{"token":"..."}`)
- Optional `HUB_INTAKE_URL` (default `https://hub.dtcdev.click/api/intake/tasks`)

### Body

```json
{
  "title": "invoice-intake failed",
  "text": "run … error …",
  "project": "dapier",
  "key": "optional-idempotency-key",
  "wake": true,
  "source": {"type": "forwarded", "reference": "dapier-run:…", "timestamp": "2026-10-19T12:00:00Z"}
}
```

On success: `202` with `hub_task_id`, `intake_key`, optional `assign` (Hub intake assign / wake).

### Direct Hub URL (Alexey)

If dapier secrets are not yet wired, create intake yourself:

```bash
curl -sS -X POST https://hub.dtcdev.click/api/intake/tasks \
  -H "authorization: Bearer $HUB_TOKEN" \
  -H "content-type: application/json" \
  -d '{"key":"manual-1","project":"dapier","title":"…","text":"…","source":{"type":"forwarded","reference":null,"timestamp":null}}'
```

Then `hub intake assign <id> …` to queue a Developer session.

## Events

| Source | How it reaches its bot |
| --- | --- |
| Explicit POST to `/hooks/developer` | Hub intake (+ assign when `wake`) |
| Workflow action `hub_intake` | Same Hub path from any workflow |
| `dev@dtcdev.click` | SES route `dev` → dapier email event → `dev-mailbox` workflow → Developer bot webhook |
| `tofu@dtcdev.click` | SES route `tofu` → dapier email event → `tofu-mailbox` workflow → Tofu Grok bot webhook |
| `orin@dtcdev.click` | SES route `orin` → dapier email event → `orin-mailbox` workflow → Orin Grok bot webhook |

Existing shared routes (`newsletter@`, `invoice@`, `todo@`, `agents@`) are unchanged.

## Per-bot addresses

| Address | SES route label | Destination (workflow) | Lands how |
| --- | --- | --- | --- |
| `dev@dtcdev.click` | `dev` | Developer bot webhook (`dev-mailbox`) | MX → SES main → S3 → processor → SNS → dapier EventQueue → `dev-mailbox` → Developer bot webhook |
| `tofu@dtcdev.click` | `tofu` | Tofu Grok bot webhook (`tofu-mailbox`) | same chain → `tofu-mailbox` → Tofu Grok bot webhook |
| `orin@dtcdev.click` | `orin` | Orin Grok bot webhook (`orin-mailbox`) | same chain → `orin-mailbox` → Orin Grok bot webhook |

SES recipient map lives in `aws-infra` / `aws-infra-opmcp` `main/email-intake` `locals.routes`. Add the two addresses there and apply (does not change MX). Publish the workflows with `dapier workflows save` / `publish` after deploy.

## How to test

1. Set secrets on the dapier API/worker (see template env vars).
2. `python -c` HMAC of `{"title":"ping","text":"hi","wake":false}` and POST to `/hooks/developer`.
3. Confirm Hub task appears: `hub intake list --project dapier`.
4. After SES routes apply: mail `dev@dtcdev.click` / `tofu@dtcdev.click` / `orin@dtcdev.click` and confirm each bot webhook POST returns 2xx (`dapier runs list --workflow <dev|tofu|orin>-mailbox` shows `http_request` 2xx).


See also [builtin-mailboxes-webhooks.md](builtin-mailboxes-webhooks.md) for the t526/t527 built-in path (preferred over custom /hooks/developer + terraform SES).
