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
Telegram webhook and its bot connection. Its stop filter prevents the hook's
synthetic workflow from running business actions a second time. The hook
remains visible as a delivery configuration and records a filtered run;
the managed `todo-intake` workflow owns the actions. Disabling or deleting
the hook would stop Telegram delivery.

These files record an operator configuration cutover, not an automatic deploy
migration. Apply them through `dapier workflows save`, `dapier workflows
publish`, and `dapier hooks save`. Hook edits preserve the webhook token
unless explicitly asked to rotate it. Keep a private export of the old
definitions for rollback and avoid overlapping the old hook action chain
with the new managed Telegram triggers during cutover.

## Multi-trigger invoice filing cutover

`multi-trigger-workflows/invoice-filing.yaml` combines email route
`invoice-attachment` and renderer `job.completed`. Its condition uploads
renderer output when present, otherwise the stored email attachments, to
the existing invoice Dropbox folder. It preserves the original input types
and renderer completion scope.

Save and publish this configuration through the workflow CLI. Turn off
`email-attachment-dataops.yaml` and `rendered-invoice-dataops.yaml` during
cutover so each event uploads once. Keep those workflows disabled for
rollback. `email-render-invoice.yaml`, `dropbox_on_upload.yaml`, and the
standalone `invoice` email trigger keep their existing behavior.

The cutover tests run the real matching and condition machinery with
connector side effects stubbed. The live per-step test API can also verify
branch selection without sending email, writing rows, or uploading files.
