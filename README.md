# dapier

A small, code-configured automation runner for AWS serverless. Connectors normalize
external events, YAML workflows match those events, and actions deliver signed HTTP
webhooks.

## Architecture

```text
YouTube WebSub / custom hooks -> API Gateway -> ingress Lambda -> SQS
SES -> Datamailer worker -> SNS -------------------------------------> SQS
                                                                    |
                                                                    v
                                                        workflow worker Lambda
                                                                    |
                                   +--------------------------------+----------------+
                                   v                                v                v
                              DataOps API                       Slack API       HTML renderer
```

Connections, app credentials, OAuth tokens, cursors, and idempotency records are
stored in DynamoDB. DynamoDB's default AWS-owned encryption protects records at
rest, while narrowly scoped IAM policies control application access. Generated
infrastructure secrets such as session signing and webhook verification values
remain in Secrets Manager. SQS provides retries and dead-letter queues; CloudWatch
alarms track worker failures and visible DLQ messages.

The proposed shared-auth OAuth token factory and agent CLI are specified in
[docs/oauth-token-factory-spec.md](docs/oauth-token-factory-spec.md).

## Configure a workflow

Add a file under `workflows/`:

```yaml
id: new-dropbox-pdf
enabled: true
trigger:
  connector: dropbox
  event: file.created
  filters:
    path:
      prefix: /incoming/
      suffix: .pdf
actions:
  - type: webhook
    url: https://example.com/hooks/new-file
    secret_id: dapier/webhooks/example
    timeout_seconds: 10
```

The webhook receives the normalized event as JSON. When `secret_id` is present,
the worker reads a Secrets Manager secret containing either a plain signing secret
or `{ "signing_secret": "..." }`, and adds `X-Dapier-Signature`, an HMAC-SHA256
signature of the request body.

Each workflow item carries its own connector config — there is no global
channel or folder list. A YouTube trigger names the channel(s) it watches in
its trigger filters (`channel_id: {equals: UC...}` for one, `channel_id:
{in: [UC..., UC...]}` for several), and the WebSub renewal job subscribes
exactly those channels. A Dropbox connection carries its own listing root
(`root_path`, editable in the console's connection dialog or set at
`dapier connections import --root-path`); empty lists the whole Dropbox.

## Workflow designer

`designer/` is a local visual editor for `workflows/*.yaml` — draw the trigger
and action chain on a node canvas, edit each node's fields in the inspector,
and **Save to git** writes the YAML file and commits it; **Push** pushes the
branch. The canvas is a hand-rolled SVG board (drag handles to connect,
reattachable arrow endpoints, pan/zoom, undo/redo) adapted from the
ai-system-design-studio project.

```sh
make designer          # install deps, start api on :8787 + vite on :5175
```

Then open http://localhost:5175. The API server talks to the git checkout it
lives in (`DAPIER_ROOT` to override); it binds 127.0.0.1 only, and
`DESIGNER_UNIX_SOCKET` switches it to a unix socket instead of TCP.

### Extending the catalog

Everything the palette and inspector know about action types lives in one
declarative file, `designer/src/catalog.ts`. When `src/engine.py` grows a new
action, append one object to `actionCatalog` — `type` (the YAML `type` value),
`label`, optional `icon` (a lucide icon or a product logo from `logos.tsx`),
and one `fields` entry per YAML key the engine
reads. `type: "number" | "boolean" | "select" | "textarea"` picks the inspector
widget and YAML coercion, and `group` nests keys under an object (e.g.
`group: "pdf"` → `action.pdf.page_format`). Nothing else needs changing:
palette, inspector, defaults, and YAML round-trip all derive from the catalog.
Action types missing from the catalog are not lost either — the designer keeps
them as opaque nodes and rewrites their YAML untouched on save.

Connectors — the products workflows hook into — are first-class too, in
`connectorCatalog` in the same file: one entry per product with `name` (the
YAML `connector` value), `label`, `logo` from `logos.tsx`, and the `events` it
emits. Each entry renders as a trigger chip in the palette's Triggers group
(separate from Actions), supplies the trigger's event suggestions, and brands
the trigger node and sidebar rows — so a trigger is always a connector's
trigger, never a bare node.

## Deploy

Prerequisites: Python 3.12, AWS SAM CLI, and configured AWS credentials.

```bash
sam build --config-env sandbox
sam deploy --config-env sandbox
```

The stack output includes the public API URL. Test the complete queue-to-webhook path:

```bash
curl -X POST "$API_URL/hooks/custom/demo" \
  -H 'content-type: application/json' \
  -d '{"hello":"world"}'
```

The sandbox deployment is available at `https://dapier.dtcdev.click`.

## Administration console

Open `https://dapier.dtcdev.click` and sign in as `admin`. Retrieve the generated
password from Secrets Manager without putting it in source control:

```bash
aws secretsmanager get-secret-value \
  --secret-id dapier/admin \
  --region eu-west-1 \
  --query SecretString \
  --output text | jq -r .password
```

The **Credentials** view accepts the Slack bot token used by Dapier and the
Mailchimp API key used by DataOps. The values are write-only: the browser sends
them over HTTPS to the administration API, which stores them in the dedicated
DynamoDB credentials table and returns only presence and update metadata. The
**Connections** view shows one card per service: click **Connect** on Google
Calendar, YouTube, or Dropbox, approve access on the consent screen, and the
token is stored — no client IDs, secrets, or scopes to enter. When a token
expires, click the card again. OAuth client IDs and secrets are shared per
provider and deploy with the stack (see below); provider tokens land in the
same write-only DynamoDB credentials table.

Slack connects without OAuth: click **Paste bot token** on the Slack card and
save a bot (`xoxb-`) or user (`xoxp-`) token — Dapier
verifies it with Slack's `auth.test`, stores it in the credentials table under
the connection's ID, and marks the connection connected. Workflows reference it
by `connection_id` (the `youtube-slack` workflow posts with `connection_id:
slack`); actions may still use a raw `credential_id` for global credentials.

OAuth clients are created once in each provider console (Google Cloud Console
for Calendar/YouTube, Dropbox App Console for Dropbox) with the redirect URI
`https://dapier.dtcdev.click/oauth/callback`. Set them in the console under
**Credentials → OAuth clients** or with `dapier oauth-clients set` — the values
are stored in the credentials table and take effect immediately, no redeploy. YouTube shares the Google
client. Rotating a client there is also how you re-webhook Dropbox: the
ingress accepts the configured secret and the deploy-time one during a
rotation window. The deploy-time environment remains a fallback for a fresh
stack — export the variables before a deploy that should seed or rotate them;
omit them and CloudFormation keeps the previous values:

```bash
GOOGLE_OAUTH_CLIENT_ID=... GOOGLE_OAUTH_CLIENT_SECRET=... \
DROPBOX_OAUTH_CLIENT_ID=... DROPBOX_OAUTH_CLIENT_SECRET=... \
  make deploy
```

The `CredentialsTableName` and `CredentialsTableArn` stack outputs allow
authorized consumers such as DataOps to receive exact-table, read-only IAM
access without sharing Dapier's administration permissions.

## OAuth token factory

The full design lives in
[docs/oauth-token-factory-spec.md](docs/oauth-token-factory-spec.md). One
deliberate deviation: OAuth tokens stay in the DynamoDB credentials table
(with optimistic versioning) instead of Secrets Manager, so all application
secrets share one storage boundary and IAM shape. The OAuth client ID/secret
pair is shared per provider and comes from deploy-time configuration rather
than per-connection entry.

Administration is open to every account that completes DTC sign-in — the DTC
identity provider only issues datatalks.club accounts. To restrict it to a
subset, configure an allowlist at deploy time:

```bash
sam deploy --config-env sandbox --parameter-overrides OperatorEmails=you@datatalks.club ...
# or several: OperatorEmails="you@datatalks.club,teammate@datatalks.club"
# stable Cognito subjects work too: OperatorSubjects="Google_123,..."
```

Install the operator CLI from this repo and sign in with the same DTC identity
(the CLI client must be registered in the shared-auth stack with its localhost
redirect URIs; the API publishes them at `/api/agent/config`):

```bash
pip install .
dapier auth login
dapier connections list
dapier connections show youtube-personal
dapier connections connect youtube-personal --agent buildcamp-uploader
dapier token exec youtube-personal --agent buildcamp-uploader -- <command>
dapier token write youtube-personal --agent buildcamp-uploader --output <private-file>
```

`token exec` puts a fresh access token only in the child's environment (as
`DAPIER_ACCESS_TOKEN` plus `YOUTUBE_ACCESS_TOKEN`/`DROPBOX_ACCESS_TOKEN`) and
never prints it. `token write` creates a `0600` file and refuses to overwrite
without `--force`. Both commands verify the returned provider account ID
against the connection's bound account before handing anything out.

Email triggers reserve `name@dtcdev.click` and run actions for every message
sent to that address. They are live immediately — no deploy. Operators manage
them with:

```bash
dapier triggers list
dapier triggers show consulting
dapier triggers save trigger.json
dapier triggers delete consulting
```

`save` takes a JSON file (or `-` for stdin) with a name, an optional
description, and one or more actions, e.g.:

```json
{
  "name": "consulting",
  "description": "consulting invoices",
  "actions": [
    {"type": "dropbox_upload", "connection_id": "dropbox",
     "folder": "_dtc_paperwork/income-invoices"},
    {"type": "dataops", "auth_secret_id": "dataops/auth"}
  ]
}
```

Action types and their keys match the workflow catalog (`webhook`, `slack`,
`telegram_send`, `email_send`, `dataops`, `dropbox_upload`, `dropbox_delete`,
`render_html_to_pdf`). Text fields accept `{field}` templates from the
triggering event; `email_send` sends through SES from the configured sender
(the `EmailSender` deployment parameter, default `no-reply@` the trigger
domain) to one or more comma-separated `to` addresses. Some
local parts are reserved (`invoice`, `no-reply`, ...), and routes already
claimed by YAML workflows cannot be shadowed.

One-time migration of the existing DataTalksClub YouTube credential (bytes are
transferred, never logged; refresh and channel ID are verified first; backups
are untouched). `--client-id`/`--client-secret-file` are optional — they are
needed only when the refresh token was issued by a client other than the
shared deploy-time one, and are then stored with the connection:

```bash
dapier connections import youtube-datatalksclub --provider youtube \
  --authorized-user-file token.json \
  --expected-account UCDvErgK0j5ur3aLgn6U-LqQ \
  --scopes https://www.googleapis.com/auth/youtube
```

Everything else the administration console can do, an operator can do from
the CLI — the commands drive the same operator-gated API as the console:

```bash
dapier overview                       # workflows, connections, credentials, recent executions
dapier credentials set slack --file slack-token.txt   # or --file - for stdin
dapier credentials set mailchimp --file mailchimp-key.txt
dapier grants list [--connection youtube-personal]
dapier grants save grant.json         # {"connection_id", "subject", "agent", "operations"}
dapier grants delete youtube-personal "subject-9#buildcamp-uploader"
dapier connections revoke youtube-personal   # revoke stored tokens
dapier oauth-clients list                    # shared OAuth clients (dropbox/google/youtube)
dapier oauth-clients set google --client-id my-id --client-secret-file -   # or a file path
```

Credential values travel only in the request body and are never echoed; like
the console's write-only credential fields, they cannot be read back.

## Connector plan

- **Dropbox:** complete. Ingress answers the verification challenge, checks
  `X-Dropbox-Signature` (HMAC-SHA256 with the deploy-time Dropbox app secret,
  failing closed when none is configured), and queues one notification per
  notified account. A resolver Lambda walks each account with
  `files/list_folder`/`files/list_folder/continue`, persists cursors and per-file
  `rev` state in the cursors table, and emits `file.created`, `file.updated`, and
  `file.deleted` envelopes onto the workflow queue. Deterministic event ids and
  last-page cursor commits make replays and crashes harmless; a dedicated
  dead-letter queue with a CloudWatch alarm catches accounts that keep failing.
- **YouTube:** WebSub subscription renewal, callback verification, and Atom
  notification ingress are live; public channel upload notifications need no
  OAuth. The renewal Lambda subscribes the channels named by the workflow
  items' `channel_id` filters (no separate channel list to keep in sync).
- **Email:** Datamailer owns SES receipt, MIME parsing, and private artifact storage;
  its normalized SNS events feed Dapier's event queue.
- **OAuth:** adding a connection is one click in the administration console:
  save it and the browser continues straight into the provider consent flow.
  OAuth clients are shared per provider and configured at deploy time; provider
  tokens are stored in the dedicated credentials table, and the general
  connections table contains non-secret connection metadata only.

Workflow files are packaged at deployment time. A deployment is therefore the audit
trail and rollback mechanism for configuration changes.
