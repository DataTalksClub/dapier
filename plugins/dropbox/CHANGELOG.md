# Changelog

## 1.0.0 — 2026-10-03

First release as a plugin. No behavior change: the upload/read/find/move/
copy/delete/temp-link actions, folder and entries discovery, the account
health check, the trigger chip, the per-event sample pull, and the
`dropbox.files` poll source registered exactly as before — now from
`plugins/dropbox/` with the plugin manifest instead of the hand-maintained
core connector lists.

The Dropbox OAuth provider adapter stays in core (`connections.providers`),
and the `dropbox_resolver` intake Lambda (webhook → delta resolution) stays
in `triggers.intake` with its API-router wiring — core must not import
plugin code.

## Unreleased

`dropbox_upload` accepts `exclude_content_types` (list or comma-separated
string): attachments with those MIME types (matched without parameters,
case-insensitive) are skipped before the upload, and `attachment_selection`
applies to what remains — so an invoice email stapled with Terms-of-Service
HTML can still upload exactly its PDF.

`dropbox_upload` accepts `skip_existing`: an HTTP 409 path conflict returns
`already_exists: true` instead of failing the step, so replaying an archive
of a file that is already at that path succeeds.
