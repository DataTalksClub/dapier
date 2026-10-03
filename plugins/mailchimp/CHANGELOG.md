# Changelog

## 1.0.0 — 2026-10-03

First release as a plugin. No behavior change: the member actions
(find/find-or-create/upsert/remove/unsubscribe/tag), audience and member
discovery, the stored-key health check, the trigger chip, the per-webhook-type
sample discovery, and the `mailchimp.members` poll source registered exactly
as before — now from `plugins/mailchimp/` with the plugin manifest instead of
the hand-maintained core connector lists.

The Marketing API HTTP helper, the stored-key resolution, and the
audience-webhook lifecycle (`register_webhook` / `list_webhooks` /
`delete_webhook`) moved to core `connections.providers.mailchimp_api` —
core (`triggers.hook_triggers`) calls them on the hook-trigger save/delete
path, and core must not import plugin code (the slack_tokens precedent).
