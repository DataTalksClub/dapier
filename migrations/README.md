# Multi-trigger TODO cutover

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
publish`, and `dapier webhooks save`. Hook edits preserve the webhook token
unless explicitly asked to rotate it. Keep a private export of the old
definitions for rollback and avoid overlapping the old hook action chain
with the new managed Telegram triggers during cutover.
