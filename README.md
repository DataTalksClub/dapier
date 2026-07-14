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

Connections, OAuth tokens, cursors, and idempotency records are stored in DynamoDB.
Secrets belong in Secrets Manager. SQS provides retries and dead-letter queues;
CloudWatch alarms track worker failures and visible DLQ messages.

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
them over HTTPS to the administration API, which stores them in AWS Secrets
Manager and returns only presence and update metadata. The **Connections** view
configures OAuth clients for Dropbox and YouTube; OAuth client secrets and tokens
use the same write-only storage boundary.

## Connector plan

- **Dropbox:** webhook verification and notification ingress are scaffolded. The next
  adapter resolves each notification with `files/list_folder_continue`, persists the
  cursor, and emits `file.created`, `file.updated`, and `file.deleted` envelopes.
- **YouTube:** WebSub verification and Atom notification ingress are scaffolded. This
  does not need OAuth for public channel upload notifications.
- **Email:** Datamailer owns SES receipt, MIME parsing, and private artifact storage;
  its normalized SNS events feed Dapier's event queue.
- **OAuth:** authenticated start/callback endpoints and connection storage are
  available from the administration console. OAuth credentials and tokens are stored
  in Secrets Manager; DynamoDB contains non-secret connection metadata only.

Workflow files are packaged at deployment time. A deployment is therefore the audit
trail and rollback mechanism for configuration changes.
