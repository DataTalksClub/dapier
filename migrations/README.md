# Legacy workflow cutover

`legacy-workflows/` is a one-time migration input, not a runtime catalog. The
engine and console now read workflow definitions from `PublishedWorkflowsTable`.

The deploy workflow runs this migration against the existing stack **before**
deploying the published-only code. It copies missing workflow definitions, expands the five
shared action chains into their workflows and stored triggers, and preserves
existing published edits, trigger credentials, enabled flags, and identities.
The command is repeatable after a partial failure. It can also be run manually
with AWS credentials that can read the CloudFormation stack and edit the five
workflow and trigger tables:

```bash
uv run --with boto3 --with pyyaml python -m scripts.migrate_legacy_workflows
uv run --with boto3 --with pyyaml python -m scripts.migrate_legacy_workflows --apply
uv run --with boto3 --with pyyaml python -m scripts.migrate_legacy_workflows
```

The final plan must report zero workflows to publish and zero flow references
to convert. The API and CLI both edit the managed store;
Git sync is optional and only records a copy when configured.

## Multi-trigger TODO cutover

`multi-trigger-workflows/todo-intake.yaml` combines email route `todo` with
the Telegram `todo` hook's message, channel-post, and callback-query events.
Each input appends one row to the existing TODO sheet and sends one DataOps
intake. Email keeps its subject/sender task label; Telegram keeps its trimmed
task text and confirmation message.

`multi-trigger-workflows/todo-telegram-ingress.json` preserves the enabled
Telegram webhook and its bot connection. Its no-op Python step keeps the
hook's synthetic workflow free of business actions. The hook
remains visible as a delivery configuration and records a completed no-op run;
the managed `todo-intake` workflow owns the actions. Disabling or deleting
the hook would stop Telegram delivery.

These files record an operator configuration cutover, not an automatic deploy
migration. Apply them through `dapier workflows save`, `dapier workflows
publish`, and `dapier hooks save`. Hook edits preserve the webhook token
unless explicitly asked to rotate it. Keep a private export of the old
definitions for rollback and avoid overlapping the old hook action chain
with the new managed Telegram triggers during cutover.

## Retired invoice email routes (2026-09-30)

The combined invoice-filing cutover was never applied; its design record is
reverted. The operator retired the two routes instead: the published
workflows `email-attachment-dataops` (invoice-attachment@) and
`email-render-invoice` (invoice-pdf@) were deleted, and the never-published
`invoice-filing` draft was discarded. Both addresses are gone from the
Emails console and `dapier emails list`; mail to them now matches no
workflow.

Inbound invoice filing continues on the surviving addresses: `invoice@`
(stored trigger — attachment to the invoice Dropbox folder, then DataOps
intake) and `dropbox-inbox@` for general attachments.
`rendered-invoice-dataops` stays enabled and files any completed renderer
job from whatever source.

Rollback: `dapier workflows save` then `dapier workflows publish` from
`legacy-workflows/email-attachment-dataops.yaml` and
`legacy-workflows/email-render-invoice.yaml`, which match the deleted
published versions aside from key order.
