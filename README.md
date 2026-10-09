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
alarms track worker failures and visible DLQ messages, and a queue-age alarm pages
when the event queue's oldest message is at least 900 seconds old (a full redrive
cycle of 5 receives at 180s visibility) while a depth alarm pages when its visible
backlog tops 100 messages — both catch a wedged-but-not-erroring consumer the DLQ
alarms never see.

The proposed shared-auth OAuth token factory and agent CLI are specified in
[docs/oauth-token-factory-spec.md](docs/oauth-token-factory-spec.md).

## Configure a workflow

Save a workflow through the console or CLI (`dapier workflows save file.yaml`).
The API stores it as a draft; `dapier workflows publish <file>` (or the
designer's Publish button) promotes it to the managed store live. For example:

```yaml
id: new-dropbox-pdf
enabled: true
actions:
  - type: webhook
    url: https://example.com/hooks/new-file
    secret_id: dapier/webhooks/example
    timeout_seconds: 10
trigger:
  connector: dropbox
  event: file.created
  filters:
    path:
      prefix: /incoming/
      suffix: .pdf
```

**Names and ids.** The `id` is the workflow's stable internal key — the file
name, API paths, run records, and hook triggers all use it, and it never
changes on its own. People see a *name*: an optional top-level `name:`
(at most 80 characters) when set, else one generated from what the flow does,
`"<trigger> → <main actions>"` — the example above reads "Dropbox file
created in incoming → Send webhook". The API returns both as `name` and
`name_source: auto|custom` on the workflow list and get. A new workflow may
leave `id:` out: the save assigns the slug of its name (`-2`, `-3`, ... when
taken). CLI commands that take a workflow accept the file, the id, or the
exact name (case-insensitive; an ambiguous name is refused).

The webhook receives the normalized event as JSON. When `secret_id` is present,
the worker reads a Secrets Manager secret containing either a plain signing secret
or `{ "signing_secret": "..." }`, and adds `X-Dapier-Signature`, an HMAC-SHA256
signature of the request body.

Failed runs email the operator by default (the `NotifyEmail` deployment
parameter) and appear on the console Home page, grouped by workflow. A
workflow can override recipients with a top-level `notify` list; an explicit
`notify: []` opts that workflow out. Each failed run sends one SES email
(workflow id, run id, failing step's error); SQS redeliveries of the same
record never re-notify:

```yaml
notify: [ops@example.com, lead@example.com]
```

A step that is allowed to fail can carry `on_fail: continue`: the failed step
is recorded as `skipped` in run history (the error message is kept on the
step, and later steps can template it as `{steps.<id>.error}`) and the rest
of the chain runs; the run itself still completes. Without the key (or with
`on_fail: halt`, the default) a failing step fails the run. The key works on
any step — connector action or logic step (`filter`, `condition`, `paths`,
`delay`, `for_each`, `digest`) alike.

The console’s Runs page combines workflow executions with incoming events, including
events no workflow handled. Inspect or replay these with `dapier runs events list`,
`dapier runs events show <event-id>`, and `dapier runs events replay <event-id>`.
The older `dapier inbox` commands and `/inbox` console links remain compatible.

Every run is recorded in run history (console → Runs, or `dapier runs list`),
and any run — failed or successful — can be re-executed with its original
trigger event via the Replay button in the run dialog or
`dapier runs replay <run-id>`. The unresolved failed runs of one workflow can
be replayed in bulk with `dapier runs replay-failed <workflow_id>` (runs whose
event data was never recorded are skipped with a reason).

A failure stops needing action two ways. When a later run of the same
workflow completes — which is what a successful replay produces — the
earlier failure is *recovered*: the API derives it, so nothing is written and
it drops out of the failure views on the next read, retroactively settling
every already-fixed failure in the ledger. A failure no rerun can settle (a
deleted workflow's last run, a negative test meant to fail) takes the **Mark
fixed** button in the run dialog or `dapier runs resolve <run-id>`. Either
way the run keeps its status, error, and place in history; only the verdict
moves. `dapier runs list --unresolved` / `--resolved` (and the console's
Failures / Fixed filters) split the two halves, and `dapier errors` and the
daily digest count only the unresolved ones.

Those filtered views read a bounded window of the ledger rather than all of
it, so the API reports whether the window reached the end: `paging.bounded`
is true when the search budget ran out first, and the console and CLI both
say so. An empty failures list under a clipped window means "none where we
looked", not "none". For the same reason a failure stays unresolved when the
completing run that would settle it has aged out of that window — mark it
fixed by hand rather than waiting for evidence that may never come.

A workflow can also retry its own transient action failures with a top-level
`retry` mapping: `attempts` is the total try count (1–5) and `backoff_seconds`
the delay between tries (1–900). The failed event is re-enqueued on the event
queue with that delay; run history shows the attempt count, and once the
attempts are exhausted the failure notification above fires as usual. Steps
that handle their own failure (`on_fail`/`on_error`) are unaffected, and a
workflow without the key gets exactly one attempt:

```yaml
retry: {attempts: 3, backoff_seconds: 120}
```

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

Home is the glance page: paused workflows, latest-run failures, and recent
results. Create and edit automations in Workflows (emails, schedules, hooks,
and polls are start methods on that page). Inspect failures in Runs, agent
tasks in Agents, and connected accounts in Connections (OAuth clients and
provider keys sit under App setup). Access and Audit live under Settings.
A workflow with storage steps (`storage_get`/`set`/`find`/`delete`) shows a
**Stored data** panel in its designer (the topbar's "Stored data" action, under
More on a phone) to list, filter, set and delete its keys; the old `/storage`
page link redirects to Workflows. The CLI reaches the same actions with
`dapier workflows`, `dapier runs`, `dapier connections`, `dapier storage`,
`dapier quota`, `dapier errors send-digest`, and `dapier audit`.

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

OAuth clients are created once in each provider console (Google Cloud project
`dtcdev-click` for Calendar/YouTube, Dropbox App Console for Dropbox, and Zoom
App Marketplace for Zoom API access) with the redirect URI
`https://dapier.dtcdev.click/oauth/callback`. Store them with
`uv run dapier oauth-clients set` or in the console under
**Credentials → OAuth clients** — the values are stored in the credentials
table and take effect immediately, with no redeploy. YouTube shares the Google
client. See the [connector guides](docs/connectors/README.md) for the current
Google consent, test-user, Dropbox access-level, Zoom OAuth, and scope requirements.
Rotating a Dropbox client there is also how you re-webhook Dropbox: the
ingress accepts the configured secret and the deploy-time one during a
rotation window. The deploy-time environment remains a fallback for a fresh
stack — export the variables before a deploy that should seed or rotate them;
omit them and CloudFormation keeps the previous values:

```bash
GOOGLE_OAUTH_CLIENT_ID=... GOOGLE_OAUTH_CLIENT_SECRET=... \
DROPBOX_OAUTH_CLIENT_ID=... DROPBOX_OAUTH_CLIENT_SECRET=... \
  make deploy
```

Zoom OAuth credentials are configured at runtime with
`uv run dapier oauth-clients set zoom` or in **Credentials → OAuth clients**;
the current deploy script does not seed Zoom OAuth credentials from
environment variables.

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

Install the operator CLI from this repo and sign in with the same DTC identity.
`dapier auth login` pairs the device: it prints a short code and opens
`https://dapier.dtcdev.click/device`, where you enter the code after signing in
through DTC (GitHub-style device flow — no localhost listener). Approving
issues the CLI a dapier-issued device session that acts as your DTC subject;
it rotates on every refresh and `dapier auth logout` revokes it server-side.
`dapier auth login --browser` uses the classic loopback flow instead (the
shared-auth stack registers `http://localhost:8471/callback` for its
`dapier-cli` client; the API publishes the client ID at `/api/agent/config`):

```bash
pip install .
dapier auth login
dapier connections list
dapier connections show youtube datatalks
dapier connections connect youtube datatalks --agent buildcamp-uploader
dapier token exec drive alexey@datatalks.club --agent buildcamp-uploader -- <command>
dapier token write youtube datatalks --agent buildcamp-uploader --output <private-file>
```

Connections are named by **service + account**: `drive alexey@datatalks.club`,
or any unique part of the account (`drive datatalks`). A bare service
(`zoom`) works when only one account has it; an ambiguous reference lists the
candidates instead of guessing. The API resolves the reference
(`GET /api/agent/connections/resolve?ref=…`), so every command that takes a
connection accepts it. Internal connection ids still resolve but are never
needed.

`token exec` puts a fresh access token only in the child's environment (as
`DAPIER_ACCESS_TOKEN` plus `YOUTUBE_ACCESS_TOKEN`/`DROPBOX_ACCESS_TOKEN`) and
never prints it. `token write` creates a `0600` file and refuses to overwrite
without `--force`. Both commands verify the returned provider account ID
against the connection's bound account before handing anything out.

Email flows are defined only in **Workflows**. The Emails console page is a
mailbox of what Dapier received and what happened to each message: handled by
a workflow (with a link to the run), failed, refused because the sender is not
allowed, or matched by no workflow. Filter by outcome; open a message for its
headers, attachment names, runs, **Replay**, and — for a refused sender —
**Allow this sender**. Beside it, the receiving addresses (patterns and
bounce/complaint watchers in the same list) and the allowed senders.
`dapier emails received [--outcome handled,failed|refused|unmatched]` prints
the same list from the same inbox API; `dapier inbox show <id>` the detail.

```bash
dapier emails received --outcome refused
dapier emails list
dapier emails show invoice                 # also accepts invoice@dtcdev.click
dapier workflows export invoice-intake     # edit the workflow YAML
dapier workflows save flow.yaml            # save a draft; live stays unchanged
dapier workflows publish invoice-intake    # promote the draft
dapier emails from list
dapier emails from add alexey@datatalks.club
```

In the console, each address links its workflow into the designer used by
Workflows; new addresses come from an email trigger with an address filter.
There is no separate email action editor and no separate email-trigger store:
`dapier emails list|show` reads the same inventory the console does, and every
edit goes through the ordinary workflow draft and publish lifecycle.

Publishing checks route ownership across the published workflows, including
disabled ones. Overlap returns 409 and leaves the draft
available to edit. Deliberate fan-out requires `allow_email_overlap: true` on the
workflow being published; set it in the designer's YAML view or workflow YAML.
The Emails list then shows all handlers for the address. Broad subscriptions
require the same explicit choice when they overlap existing routes.

One sender list applies to every incoming message, including Datamailer SNS
events. It starts as `alexey.s.grigoriev@gmail.com` and
`alexey@datatalks.club`. A sender outside the list runs no actions; an empty list
ignores everyone. Additional workflow filters AND with the address rule. Sender
admission is separate from receiving-address configuration and outbound senders.

An `agent` action queues a prompt for a Dapier worker — a machine running
`dapier worker`, which picks the task up and runs a headless Claude Code
process there, reporting the terminal result through the authenticated API.
Tasks stay queued until a worker is running; the console's **Workers** tab
and `dapier workers list` show which workers are active. An agent action can
sit on an email, a webhook, a schedule, or a poll.
The host needs a dedicated Dapier token, not AWS credentials or Aplexer; see
[headless worker setup](docs/headless-worker.md). Webhooks are managed with
`dapier webhooks`. A sample of what a flow receives is `dapier workflows sample`.

A receiving address is a workflow trigger, for example:

```yaml
id: consulting-intake
enabled: true
actions:
  - id: forward
    type: dataops
    auth_secret_id: dapier/dataops
    url_env: DATAOPS_INTAKE_URL
trigger:
  connector: email
  event: message.received
  filters:
    route: {equals: consulting}
```

Action types and their keys match the workflow catalog (`webhook`, `slack`,
`telegram_send`, `email_send`, `dataops`, `dropbox_upload`, `dropbox_delete`,
`render_html_to_pdf`, `code`). Text fields accept `{field}` templates from the
triggering event; `{trigger.field}` reaches the same data (plus envelope
scalars like `trigger.connector` and `trigger.occurred_at`), and
`{steps.<action_id>.output.<path>}` / `{steps.<action_id>.status}` reference an
earlier step in the same run. A `|` pipes the value through formatters —
`trim`, `lower`, `upper`, `slice:start:end`, `replace:old:new`,
`regex_extract:pattern`, `round:digits`, `format:spec` (number),
`date_format:strftime`, `date_offset:1d|-2h` — e.g.
`{trigger.subject | trim | upper}`. Missing values render as empty; unknown
formatters are rejected when the workflow or trigger is saved.
`email_send` sends through SES from the configured sender
(the `EmailSender` deployment parameter, default `no-reply@` the trigger
domain) to one or more comma-separated `to` addresses. Receiving addresses use route filters in workflows; outbound sender identities
are configured independently.

Besides messages, a workflow can watch SES feedback: `event: bounce.received`
or `event: complaint.received` fires when SES reports a bounce
or a complaint for any address at the trigger domain. Watchers need no
reserved address — the name is identity only — and match the whole domain's
feedback; an optional `filters` object scopes them (e.g.
`bounce_type: {equals: Permanent}`; feedback carries no receiving route). Feedback arrives through the
`/hooks/ses-notifications` SNS endpoint: point the SES configuration set's
feedback destination at `https://<domain>/hooks/ses-notifications`, set the
`SesConfigurationSet` deployment parameter to that set's name — every
workflow send carries it (`SES_CONFIGURATION_SET`), and SES only reports
bounce/complaint feedback for sends that name a configuration set — and set
the `SesNotificationTopics` deployment parameter (the
`SES_NOTIFICATION_TOPICS` allowlist) to the topic ARNs SES publishes to —
empty accepts any topic, which is fine for development but should be set in
production.

### Webhook, Telegram, and schedule triggers

Webhook and Telegram triggers work like email triggers but are invoked over
HTTP. A webhook trigger reserves `https://dapier.dtcdev.click/hooks/webhook/{name}`
and answers only calls carrying its bearer token; a Telegram trigger binds a
Telegram bot connection to `/hooks/telegram/{name}` (one webhook per bot, so
one connection drives at most one trigger). Both are created and listed with
`dapier webhooks save|list|show|delete`, fire the same action catalog as email
triggers, and are live immediately — no deploy.

Every accepted delivery lands in a delivery log (the trigger inbox, 30 days)
with its request headers — credentials redacted — size, response, and what
the workflows did with it. The console's Workflows › Hooks tab shows it
beside each endpoint's URL, verification, and the workflows it starts;
`dapier webhooks deliveries [name]` and `dapier webhooks delivery <id>` read
the same log, `dapier webhooks replay <id>` re-sends one, and
`dapier webhooks test <name> [--data payload.json]` POSTs a sample through
the hook's real intake with its own token or signature (a real delivery:
matched workflows run).

A webhook trigger created or updated with an optional `secret` (a plain JSON
field, so `dapier webhooks save webhook.json` passes it straight through;
`"secret": ""` clears it, an edit that omits it keeps it) locks the URL
behind a shared-secret HMAC check instead of the bearer token: callers must
send `X-Dapier-Signature: sha256=<hex>` where `<hex>` is the lowercase
HMAC-SHA256 of the **raw request body** (the exact bytes as received, before
any parsing) keyed with the secret. A bare hex value without the `sha256=`
prefix is accepted too. The comparison is constant-time, and the same header
and scheme the outbound `webhook` action signs with, so one scheme serves
both directions:

```bash
signature=$(printf '%s' "$BODY" | openssl dgst -sha256 -hmac "$SECRET" | awk '{print $NF}')
curl -s -X POST "$URL" -H "content-type: application/json" \
  -H "X-Dapier-Signature: sha256=$signature" -d "$BODY"
```

When a secret is set, a valid signature is sufficient on its own (the bearer
token is not consulted), and the bearer token alone no longer admits a
delivery: a missing header or a mismatched digest answers
`401 {"error": "invalid signature"}` and nothing is queued or run. Trigger
responses expose the lock as `"signed": true` (plus
`"signature_header": "x-dapier-signature"`) and never echo the secret.
Without a secret, behavior is unchanged: the bearer token gates the URL and
any `X-Dapier-Signature` header is ignored.

Schedule triggers are cron jobs: `dapier schedules save` takes a name, a
`cron(...)` or `rate(...)` expression, and actions, and programmatically
creates (or reprograms) an EventBridge rule `dapier-schedule-{name}`
targeted at the worker. `schedules delete` removes the rule; disabling a
trigger disables it. The rules fire without any deploy, and the CLI, the
console API (`/api/agent/schedule-triggers`, `/api/admin/schedule-triggers`),
and the worker all see the same stored actions.

The console's Schedules tab and the CLI answer "when do things run, and did
they?" from the same API: every fire is noted on its schedule (ran, fired
with no workflow listening, failed), each schedule carries a plain-language
reading of its expression, its next fires, the workflows a fire reaches, and
a health verdict that flags a schedule that went quiet ("Last fired 3 days
ago; expected every hour"):

```bash
dapier schedules list              # health and next fire per schedule
dapier schedules show morning-digest   # timing, workflows, recent fires and runs
dapier schedules upcoming --days 7 # fires coming up across all schedules
dapier schedules run morning-digest    # Run now (a manual fire via the event queue)
dapier schedules pause morning-digest  # / resume: the rule's state, noted in history
```

Poll triggers check a source (an API, RSS feed, Drive/Dropbox folder, sheet,
...) on a schedule and start their workflow once per new item. The console's
Workflows › Polls tab is their health monitor, and the CLI reaches the same
API:

```bash
dapier polls list                      # health, last check, last new item, workflows it starts
dapier polls show blog-feed            # one poll: last error, position, resets it supports
dapier polls activity [blog-feed]      # items polls picked up and the runs they started
dapier polls check blog-feed           # Poll now (queued on the worker)
dapier polls pause blog-feed           # or resume; the position is kept
dapier polls reset blog-feed --now     # skip everything waiting
dapier polls reset blog-feed --from 2026-10-01   # timestamp-positioned polls: re-read from a date
dapier inbox show <inbox-id>           # a picked-up item; `dapier inbox replay <id>` re-runs it
```

Each check records its outcome (`pollstat#<name>` in the cursors table), so
health reads `ok`, `failing` (with the last error), `late` (a rate schedule
missed three checks), `paused`, or `waiting` (not checked yet).

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
dapier overview --section workflows   # only this block; returns JSON
dapier credentials set slack --file slack-token.txt   # or --file - for stdin
dapier credentials set mailchimp --file mailchimp-key.txt
dapier grants list [--connection youtube-personal]
dapier grants save grant.json         # {"connection_id", "subject", "agent", "operations"}
dapier grants delete youtube-personal "subject-9#buildcamp-uploader"
dapier connections revoke youtube-personal   # revoke stored tokens
dapier connections create youtube-team --provider youtube --scopes https://www.googleapis.com/auth/youtube.readonly
dapier connections connect youtube-team --agent my-agent   # open consent in Chrome
dapier connections edit dropbox --root-path /incoming
dapier connections edit dropbox --scopes account_info.read files.metadata.read files.content.read files.content.write
dapier oauth-clients list                    # shared OAuth clients (dropbox/google/zoom; youtube shares google)
dapier oauth-clients set google --client-id my-id --client-secret-file -   # or a file path
dapier oauth-clients set zoom --client-id my-id --client-secret-file -
dapier tokens list                           # operator-issued API tokens (no secrets)
dapier tokens create --name personal-scheduler --agent personal-scheduler
dapier tokens revoke personal-scheduler
dapier tokens delete personal-scheduler      # permanently remove a revoked token (and its grants)
dapier webhooks save webhook.json            # webhook and Telegram callbacks (see below)
dapier webhooks test orders --data body.json # send a test request, then follow it:
dapier webhooks deliveries orders            # recent deliveries and their outcome
dapier schedules save schedule.json          # {"name", "expression": "cron(0 8 * * ? *)", "actions"}
```

Credential values travel only in the request body and are never echoed; like
the console's write-only credential fields, they cannot be read back.
After changing requested scopes, reconnect that connection to grant them. See
[the shared scope-change procedure](docs/connectors/README.md#changing-requested-scopes)
and the [Google](docs/connectors/google.md) or
[Dropbox](docs/connectors/dropbox.md) provider guide for provider-specific
permission steps and the source files to update when changing defaults.

### API tokens for headless consumers

For machines that call the agent API unattended (the personal scheduler, CI
jobs), an operator issues long-lived **API tokens** instead of enrolling a
browser identity: `dapier tokens create --name <id> --agent <agent>` (or the
console's API tokens view) returns a `dap_…` bearer value exactly once. The
token authenticates as the subject `token:<id>`, is bound to one agent name,
and its reach is governed by the ordinary grants table — grant
`token:<id>` on specific connections with `dapier grants save`, and revoke it
any time with `dapier tokens revoke` (or the console). Only the SHA-256 hash
is stored; presented values stop authenticating the moment the token is
revoked. Old revoked entries can be cleaned out of the list with
`dapier tokens delete` (or the console's Remove button) — deleting a revoked
token also deletes its grants. API tokens never qualify for operator actions.

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

The console opens after the operator check and loads overview sections independently.
Workflows, recent activity, usage, connections, credentials/OAuth clients, API
tokens, and email settings each use `GET /api/admin/overview?section=<section>`.
Each section renders as its response arrives; failures stay local with a Refresh
retry. The agent route accepts the same selector (`dapier overview --section
workflows|activity|usage|connections|credentials|tokens|emails`). Omitting
`section` preserves the full overview response for existing clients.
