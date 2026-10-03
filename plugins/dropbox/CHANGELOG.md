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
