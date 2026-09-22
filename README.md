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
**Connections** view configures OAuth clients for Dropbox and YouTube; OAuth
client secrets and tokens use the same write-only storage boundary.

The `CredentialsTableName` and `CredentialsTableArn` stack outputs allow
authorized consumers such as DataOps to receive exact-table, read-only IAM
access without sharing Dapier's administration permissions.

## OAuth token factory

The full design lives in
[docs/oauth-token-factory-spec.md](docs/oauth-token-factory-spec.md). One
deliberate deviation: OAuth client secrets and tokens stay in the DynamoDB
credentials table (with optimistic versioning) instead of Secrets Manager, so
all application secrets share one storage boundary and IAM shape.

Administration requires an explicit operator allowlist — a DTC sign-in alone
is not enough. Configure it at deploy time (empty denies everyone):

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

One-time migration of the existing DataTalksClub YouTube credential (bytes are
transferred, never logged; refresh and channel ID are verified first; backups
are untouched):

```bash
dapier connections import youtube-datatalksclub --provider youtube \
  --client-id <id> --client-secret-file secret.txt \
  --authorized-user-file token.json \
  --expected-account UCDvErgK0j5ur3aLgn6U-LqQ \
  --scopes https://www.googleapis.com/auth/youtube
```

## Connector plan

- **Dropbox:** complete. Ingress answers the verification challenge, checks
  `X-Dropbox-Signature` (HMAC-SHA256 with the configured connections' app secrets,
  failing closed when no Dropbox connection exists), and queues one notification per
  notified account. A resolver Lambda walks each account with
  `files/list_folder`/`files/list_folder/continue`, persists cursors and per-file
  `rev` state in the cursors table, and emits `file.created`, `file.updated`, and
  `file.deleted` envelopes onto the workflow queue. Deterministic event ids and
  last-page cursor commits make replays and crashes harmless; a dedicated
  dead-letter queue with a CloudWatch alarm catches accounts that keep failing.
- **YouTube:** WebSub subscription renewal, callback verification, and Atom
  notification ingress are live; public channel upload notifications need no OAuth.
- **Email:** Datamailer owns SES receipt, MIME parsing, and private artifact storage;
  its normalized SNS events feed Dapier's event queue.
- **OAuth:** authenticated start/callback endpoints and connection storage are
  available from the administration console. OAuth credentials and tokens are stored
  in the dedicated credentials table; the general connections table contains
  non-secret connection metadata only.

Workflow files are packaged at deployment time. A deployment is therefore the audit
trail and rollback mechanism for configuration changes.
