# Zapier Migration Plan

## Goal

Replace the selected Zapier workflows with small AWS serverless components while
keeping product-specific behavior in the product that owns it. Dapier coordinates
events and actions; it does not become an email server, document store, Slack bot,
or browser-rendering runtime.

## Selected Workflows

| Workflow | Decision | Replacement |
| --- | --- | --- |
| YouTube channel to Slack | Keep | YouTube WebSub -> Dapier -> Slack API using the existing bot token |
| Telegram TODO | Keep, modify | Existing Telegram bot -> DataOps intake directly |
| Invoice email attachment | Keep, modify | Datamailer inbound -> Dapier -> DataOps artifact |
| TODO email | Keep, modify | Datamailer inbound -> Dapier -> DataOps intake |
| Invoice email body to PDF | Keep, modify | Datamailer -> Dapier -> HTML Renderer -> DataOps artifact |
| Eventbrite to Google Calendar | Remove | None |
| Edited podcast Dropbox notification | Remove | None |
| Dropbox invoice landing folder | Remove | Upload directly to DataOps |
| Google Drive mailing-list backup to S3 | Remove | DataOps-managed provider export |

Disabled YouTube-to-Twitter, invoice-picture conversion, and Finom workflows remain
out of scope.

## Target Architecture

```text
YouTube WebSub ---------------------------> Dapier -----> Slack API

SES -> Datamailer inbound S3/worker ------> Dapier -----> DataOps intake/artifacts
                                                |
                                                +-------> HTML Renderer
                                                              |
                                                              +-> private S3 PDF
                                                              +-> completion event -> Dapier

Telegram bot -----------------------------------------------> DataOps intake
```

Dapier uses SQS for durable delivery and DLQs, DynamoDB for connection state,
cursors, and idempotency, Secrets Manager for credentials, and versioned event
contracts. Large HTML, MIME messages, attachments, and rendered PDFs move through
private S3 references rather than Lambda payloads.

## Repository Ownership

### `dapier`

- Own the normalized connector envelope and YAML workflow definitions.
- Implement YouTube WebSub subscription/renewal and notification normalization.
- Consume Datamailer's versioned inbound-email event.
- Orchestrate attachment invoices, TODO emails, and email-body PDF jobs.
- Invoke the renderer asynchronously and resume on its completion event.
- Call the Slack API and authenticated DataOps APIs.
- Read the existing Slack bot token from Secrets Manager; do not copy it into YAML.
- Add idempotency, retry classification, DLQs, metrics, and alarms.
- Do not parse raw MIME, run Chromium, or store business documents.

### `AI-Shipping-Labs/banner-generator`

- Extract the generic Playwright/Chromium runtime into an HTML Renderer service.
- Keep banner templates, variable substitution, and banner-specific sizing in Banner Generator.
- Submit final HTML to the renderer through an IAM/SQS job contract.
- Preserve current PNG, JPEG, and PDF output during migration.
- Retire the bearer-token Function URL only after compatibility users migrate.

### HTML Renderer service

- Accept private S3 HTML input and write PDF/PNG/JPEG output to private S3.
- Support document page sizes and margins in addition to image viewports.
- Deny JavaScript, external network access, and local file access by default.
- Emit versioned completion/failure events and make output keys idempotent.
- Run as a Lambda container; callers remain lightweight Lambda functions.

This should be a service boundary rather than a shared Python dependency. It avoids
shipping Chromium to callers and avoids synchronized package releases across the
AI Shipping Labs and DataTalksClub projects.

### `DataTalksClub/datamailer`

- Own SES receipt, raw MIME storage, parsing, alias routing, and retention.
- Reuse the existing inbound MIME inspection parser and fixtures.
- Publish sender, recipients, subject, dates, Message-ID, text/HTML references, and
  attachment references in a versioned event.
- Use Message-ID plus recipient route for idempotency.
- Do not render PDFs or apply DataOps invoice/TODO semantics.

Datamailer's production Terraform lives outside the application repository today.
The change must explicitly update the corresponding `aws-infra` Datamailer root for
the receipt-rule-to-S3-to-worker path and permissions.

### `DataTalksClub/dataops`

- Expose one authenticated machine-to-machine intake contract for email metadata
  and S3 artifact references.
- Accept original attachments and rendered email PDFs as private/sensitive artifacts.
- Deduplicate using source Message-ID and route, and link artifacts to intake items.
- Use the existing Telegram/email intake and artifact foundations instead of adding
  parallel storage concepts.
- Implement mailing-list exports separately under issue #108.

### `DataTalksClub/aws-infra`

- Provision the Dapier SQS/DLQ, DynamoDB, Lambda, API Gateway, S3, IAM, and alarms.
- Provision or extend Datamailer inbound SES/S3 notifications and worker permissions.
- Provision the renderer ECR/Lambda/queues/buckets and least-privilege cross-service IAM.
- Prefer IAM invocation and bucket policies over public Function URLs and shared tokens.

## Contracts

All contracts include `schema_version`, an immutable event/job ID, `occurred_at`,
source identity, and a trace/correlation ID. Consumers reject unsupported major
versions and ignore additive fields.

The renderer job uses S3 references:

```json
{
  "schema_version": "1.0",
  "job_id": "email:<message-id>:invoice-pdf",
  "input": {"bucket": "private-input", "key": "render/jobs/id.html"},
  "output": {"bucket": "private-output", "key": "render/jobs/id.pdf"},
  "format": "pdf",
  "pdf": {"page_size": "A4", "print_background": true},
  "security": {"javascript": false, "network": "deny"}
}
```

The DataOps request carries metadata and S3 references, never attachment bytes,
signed URLs, OAuth tokens, or raw secrets.

## Delivery Order

1. Extract and deploy the generic HTML Renderer; migrate Banner Generator to it.
2. Finalize the DataOps email-document intake contract.
3. Implement Datamailer inbound processing and normalized events.
4. Implement Dapier email orchestration and DataOps actions.
5. Complete Dapier's YouTube trigger and Slack action using the existing bot token.
6. Implement DataOps mailing-list exports independently.
7. Shadow-run each workflow, verify idempotency and outputs, then disable its Zap.

## Tracking Issues

- Banner Generator: [AI-Shipping-Labs/banner-generator#2](https://github.com/AI-Shipping-Labs/banner-generator/issues/2)
- Datamailer: [DataTalksClub/datamailer#80](https://github.com/DataTalksClub/datamailer/issues/80)
- DataOps email/document intake: [DataTalksClub/dataops#109](https://github.com/DataTalksClub/dataops/issues/109)
- DataOps mailing-list exports: [#108](https://github.com/DataTalksClub/dataops/issues/108)

## Implementation Status

Deployed in the sandbox AWS account:

- Dapier API, WebSub renewal/verification, YAML workflows, SQS/DLQs, idempotency,
  renderer orchestration, DataOps actions, and CloudWatch alarms.
- Datamailer SES receipt, alias routing, normalized events, private attachment
  storage, and retention.
- Generic HTML renderer Lambda container and completion events.
- DataOps authenticated email-document intake and provider-neutral mailing-export
  runtime. Mailing exports remain disabled until a Mailchimp secret and account
  configuration are supplied.

Live end-to-end checks cover TODO email, invoice attachment, and rendered invoice
PDF ingestion into DataOps. YouTube WebSub is subscribed and signature-verified;
the Slack action remains disabled in practice until the existing Automator bot
token is copied into the `dapier/slack` Secrets Manager secret.
