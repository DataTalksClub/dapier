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
| Telegram TODO | Keep, modify | Native bot extension plus Date/Text webhook -> TODO sheet |
| Invoice email attachment | Keep, modify | Datamailer -> Dapier -> Dropbox archive and DataOps artifact |
| TODO email | Keep, modify | Datamailer -> Dapier -> TODO sheet and DataOps intake |
| Invoice email body to PDF | Keep, modify | Datamailer -> Dapier -> HTML Renderer -> DataOps artifact |
| Eventbrite to Google Calendar | Remove | None |
| Edited podcast Dropbox notification | Remove | None |
| Dropbox invoice landing folder | Keep, modify | Date-prefix and archive, then forward to DataOps |
| Google Drive mailing-list backup to S3 | Keep | Copy exported files from the shared folder to S3 |

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

## Earlier sandbox implementation status

This section records the earlier implementation. Current migration definitions
and remaining connection checks are listed below.

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

## Concrete configuration from the 2 October 2026 inspection

The six definitions in `workflows/*.yaml` carry the original Zap IDs in their
`zapier-<id>` tags. They are inactive migration definitions: saving through
`dapier workflows save` creates drafts for the existing workflow IDs without
changing their published behavior. The Telegram catch hook and mailing backup
have their own workflow IDs. Operator trigger configuration is in
`workflows/zapier/triggers/`; every trigger there is disabled.

| Zap ID | Definition | Applied configuration |
| --- | --- | --- |
| 157386985 | `mailing-list-backup.yaml` | Drive `0AJbu0ZbG97XkUk9PVA`, folder `1-MAoAuQbny7FK9UQuk8800zT8M8TOQ59`, every 2 minutes; download bytes to S3 `datatalks-mailchimp-backup`, original title as exact object key, leading slash removed. No extension filter. |
| 153709869 | `todo-intake.yaml` | Workbook `1xdeCQOLRS4vodv3GjaXNaC6t63-qdqL3X98KqJFs0dw`, worksheet ID `0`; Date from processing time, Task `Process email "<Subject>" from <From>`, Notes blank, Status `NEW`. Existing native Telegram tasks and confirmations remain an extension. |
| 153562936 | `invoice-intake.yaml` | Email route `invoice`; UTC date from the email Date header; unchanged attachment bytes uploaded to `/_dtc_paperwork/invoices/<UTC date>-<Subject>.pdf`; overwrite disabled; then DataOps intake. |
| 153485577 | `telegram-todo.yaml` | A catch hook named `telegram-todo`, using the request's `Date` and `Text` fields; same TODO workbook and worksheet ID `0`, Notes blank, Status `NEW`. Sender Date is retained. |
| 110871466 | `youtube-slack.yaml` | Channel `UCDvErgK0j5ur3aLgn6U-LqQ` via WebSub; Slack `C01BQC114P2`, display name `YouTube`, original message segment order, link/media unfurling and name linking enabled, reply broadcast disabled. |
| 155120966 | `dropbox_on_upload.yaml` | Poll `/_dtc_paperwork/invoices-landing` every 2 minutes; rename to `<processing date>-<original filename>` preserving the extension once; move to `/_dtc_paperwork/invoices`; forward the archived path to DataOps. No delete step or extension filter. |

The folder poll identifies items by their stable Dropbox file ID. The workflow
matches that named poll, so account webhook deliveries cannot cause a second
execution of the same migration. Both folder polls seed their cursors without
replaying existing files. Moving a file out of the landing folder also prevents
it from being picked up again there.

### Configurable adapter options

- `date_time` accepts an optional offset-bearing ISO or email timestamp,
  an IANA `timezone` and a strftime `format`. Omitting `value` reads processing
  time. Outputs are `iso`, `formatted` and `timezone`.
- `dropbox_upload` renders `folder` and `filename` from the event and previous
  steps. `attachment_selection` accepts `all`, `single` or `first`.
  `single` rejects ambiguous multi-attachment emails before any upload.
  `overwrite` and `autorename` are separate options. Existing flows retain
  their defaults: all attachments, no overwrite, autorename enabled.
- `dataops.path` can reference the completed move step instead of attempting
  to download the missing original path.
- `sheets_append_row.sheet_id` selects the numeric worksheet ID and resolves
  its current title; it overrides `sheet_name` and survives tab renames.
- Structured Sheets rows and S3 source references can be edited as JSON in
  the designer and remain objects/arrays when saved through the shared API.
- `slack.username`, `link_names` and `reply_broadcast` expose the corresponding
  provider options. A modern Slack app needs `chat:write.customize` for a
  display-name override, as described in the
  [Slack posting documentation](https://docs.slack.dev/reference/methods/chat.postMessage/).
- `s3_upload.key_mode: exact` preserves the source title rather than applying
  filename sanitization. `omit_content_type: true` omits Content-Type;
  otherwise `content_type` can explicitly specify a literal value.
- `google-drive.files` polls accept a `drive_id`, include shared-drive items
  and retain pagination. Console advanced options, CLI trigger JSON and the
  HTTP API all preserve this setting.

### Adjustments and remaining source-provider checks

Invoice sheet appends remain in DataOps' domain. Dapier does not recreate the
bookkeeping worksheet or price placeholders. The original downstream mappings
were workbook `1jIBou5XvBY3uy7dsxDUVM4yiPZAgXUN5AZJN3bDJgHU`, worksheet ID
`819898795` (2022), email prices `-TODO`, landing-folder prices `TODO`.
Those values are reference requirements for DataOps if the legacy sheet remains
part of its output; this change does not claim that output is configured there.

Email TODO uses fixed CST, UTC−06:00, through `Etc/GMT+6`. Published Zap153709869
uses `Current time: CST (ISO)`; Zapier documents CST and CDT as separate fixed
offsets. The earlier `America/Chicago` candidate was incorrect and is superseded.
Six midnight/DST boundary cases and actual Gmail row110/accepted DataOps receipt
passed after live v8. Landing-folder processing dates use UTC, matching original
Python `datetime.now()` and Zapier's documented UTC Code-action runtime; the
unconditional date prefix and extension handling also match the source.

Original singular Attachment hydration is now provider-verified through an
inactive isolated copy: one PDF remains a PDF; multiple PDFs and inline PNG plus
PDF bundle into ZIPs and append `.zip` to the requested `.pdf` name. Original
Overwrite No succeeds as a same-byte no-op and autorenames changed-byte uploads.
Original Dropbox Rename and Move also autorename on changed-byte collisions,
retaining source bytes/ID and preserving the existing target. Dapier intentionally
requires exactly one stored attachment and rejects filename conflicts with strict
409 before further intake. These fail-closed rules are explicit migration
adjustments, not identical Zapier adapter behavior.

The backup preserves literal `content_type: none`. Metadata-only HEAD of the
pre-migration January 2025 archive, matched to original Drive filename/size and
creation metadata, confirms actual Content-Type `none`; no customer content was
downloaded. This resolves the original MIME wire comparison. A fully paginated
metadata-only read of the current watched folder found 82 files (52
`application/x-zip-compressed`, 30 `application/zip`), no Google-native documents
and no folders. Google-native export format remains a conditional future-input
limitation if such files are later introduced, not an unresolved comparison for
the current ZIP inputs. Future exporter create/replace/repeated-name behavior
still requires owner evidence: owned-repo searches found imports/tests/docs and
an assumed-role policy, not the producer definition.

The original Telegram bridge sender is unknown; native Telegram remains the
supported primary path. Isolated JSON/form authentication, parsing and stable-ID
retry tests passed. Existing catch-hook compatibility is retained unchanged;
text/date alone must not deduplicate intentional repeats. Actual legacy-sender
retry/cutover is conditional on a known sender-supplied request-ID contract and
does not block native Telegram's update-ID identity.

Published source Slack formatting is confirmed: two newlines around Title,
`Link: ` plus Play Url, YouTube bot name, no automation link, expanded links,
name linking and no broadcast. Migrated configuration and retained synthetic
provider output match. YouTube WebSub intentionally replaces two-minute polling;
authenticated synthetic HTTP ingress/signature/retry tests delivered exactly once
in the isolated test channel. Genuine publisher-to-hub delivery still requires a
legitimate future owner upload. Dapier's pause after five consecutive failures
remains its own policy, not Zapier's undisclosed error-ratio threshold.

See [the completion report](zapier-integration-completion-20261003.md) for provider
proofs, isolated actual-worker/live-provider DataOps publication, documented
adjustments and cleanup. Production financial ledger publication was not performed.

### Validation and activation checklist

Synthetic events live in `workflows/zapier/fixtures/`. Acceptance tests cover
filenames, unchanged bytes, exact resource IDs, dates, Slack request options,
worksheet rename handling, CLI/API draft saving and failure recovery after file
moves. Successful file steps stay deduplicated after their leases expire,
and retries restore saved outputs for downstream path templates.

Completed access/contract checks and remaining production cutover gates:

1. Verify access to the exact shared Drive folder and TODO workbook. The current
   Google connection identifies `alexey@datatalks.club`; the source used
   `alexey.s.grigoriev@gmail.com`. Resource IDs, rather than account labels,
   define the destination, but access must be checked.
2. Connect Dropbox and Slack, grant the display-name scope, and configure the
   target S3 credential through the Console or CLI. Verify DataOps intake.
3. Timezone, attachment, collision and MIME source checks are resolved above.
   Native Telegram is primary. Required DataOps publication integration passed
   against isolated live providers/local ledger; no production ledger publication
   or real payment attestation is claimed.
4. Inspect each draft against live with `dapier workflows draft-diff <id>`.
   Use strict dry-runs and per-step tests with synthetic prior outputs for
   dependencies. Dry-runs cannot verify provider defaults or permissions.
5. Existing enabled migrations have completed their live tests. The remaining
   production Dropbox cutover needs the separate authorization/producer transition
   below; do not enable another overlapping consumer to close a test checkbox.
   Existing Zapier senders and originals were never changed by draft/test work.


### Production Dropbox landing cutover plan

Automatic polling was verified with an isolated, exact-filename synthetic
workflow. Production `invoice-landing` polling remains disabled. The current
live `dropbox_on_upload` consumer monitors `/_dtc_paperwork/income-invoices/`
and performs intake/delete; the landing-folder migration draft is not live.
Do not enable a second business consumer over the same files.

For a separately authorized production cutover:

1. Save the current published workflow and poll configuration for rollback,
   inventory landing-file metadata, and confirm which service uploads files.
2. Pause uploads for the cursor-seeding window. Disable the old consumer, then
   replace the same workflow ID with the reviewed four-step landing draft
   (UTC date, rename, move, DataOps intake), initially disabled. Do not retain
   its old delete action or create another overlapping workflow.
3. Keep the production poll pointed at `/_dtc_paperwork/invoices-landing` with
   `rate(2 minutes)`. Seed its cursor while uploads remain paused; historical
   files need an explicitly reviewed backlog plan rather than automatic replay.
4. Enable the corrected workflow after seeding, then resume uploads to the
   landing folder. Verify a fresh unique synthetic file produces one run,
   matching archived bytes, absent landing paths and one DataOps receipt.
5. For rollback, disable the poll and replacement workflow before restoring
   the saved consumer/configuration. Do not replay events or delete archives
   automatically.

This plan has not been executed; existing production consumers remain unchanged.
Native Telegram is the supported primary TODO path. The original catch-hook
sender is unknown, so the existing hook remains unchanged for compatibility.
Stable request IDs are required before enabling retry deduplication for that
sender; this limitation does not block native Telegram's update-ID path.

Invoice archives use `overwrite: false`, `autorename: false`, and
`strict_conflict: true`. Dropbox otherwise accepts an identical-byte upload as
a successful no-op; strict conflict rejects that case as well as changed bytes.
This is an intentional migration adjustment: original copied-adapter tests
verified same-byte successful no-op and changed-byte upload autorename, plus
Rename/Move autorename. Strict rejection preserves existing destinations and
prevents silently creating alternate invoice archive paths. See [Dropbox's upload specification](https://github.com/dropbox/dropbox-api-spec/blob/main/files.stone).

Drive file polling excludes folders at the provider query and response boundary.
A new subfolder is not a downloadable backup file; native document files remain
eligible. Creation polling uses file identity and creation time, so replacing
contents preserves the original event, while a distinct file with the same name
produces a new event and replaces the configured exact S3 key.

The selected body-to-PDF path is also included: disabled drafts
`invoice-body-render` and `invoice-body-completion` route `invoice-pdf` through
the existing renderer, archive the completed PDF with UTC date/subject, and
forward it to DataOps. Read-only source inspection of inactive Zap153699998 confirms Raw Date→UTC
YYYY-MM-DD, Body Html→ConvertAPI PDF (`test.html` input), File URL→Dropbox with
Output-Raw Subject.pdf and Overwrite No, then Sheets. Actual no-attachment HTML
email/native renderer artifact and archive/intake were verified. Native rendering
and DataOps ownership are explicit adjustments; PDF-engine byte identity is not
claimed. Both migration definitions remain disabled.
Before enabling the pair, disable the broad `rendered-invoice-dataops` consumer
and drain its runs. Confirm no second consumer watches the selected archive
folder. Restore the old completion definition if rolling back, after pausing
the new incoming and completion pair; never overlap the consumers.

### Remaining owner decisions and current inventory

Current API inventory confirms `mailchimp-drive-backup` enabled at two minutes,
`invoice-landing` disabled at two minutes, and `dropbox_on_upload` enabled on
`/_dtc_paperwork/income-invoices/` with intake/delete. The reviewed landing draft
targets `/_dtc_paperwork/invoices-landing`; a production workflow named
`invoice-landing` is not published. That poll ID must not be mistaken for an
already deployed replacement workflow. Safe rollback/cursor tests passed in
isolation. Remaining cutover work requires identifying the file producer and
folder transition, choosing historical-backlog handling and approving a window
to pause uploads, disable/drain the old consumer, seed and enable one replacement.
There is no additional fixture test that can substitute for those decisions.

The other remaining acceptance inputs are owner evidence for future upstream
export behavior and a genuine future YouTube upload. No public upload or
production cutover was performed to force acceptance. Direct scheduler
rule/target/log diagnostics remain access-limited; authenticated hub lease state
and expiry are verified separately, not a false pass of automatic invocation logs.
