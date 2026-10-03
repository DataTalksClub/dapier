# Changelog

## 1.0.0 — 2026-10-03

First release as a plugin. No behavior change: the meeting/webinar/
recording/registrant actions, connection-scoped discovery, the health
check, the per-event webhook samples, the trigger chip, and the
`zoom.recordings` poll source registered exactly as before — now from
`plugins/zoom/` (connector package + runners package + a thin plugin.py
entry) with the plugin manifest instead of the hand-maintained core
connector lists.

`connections/zoom.py` (the OAuth connection setup helper) stays in core —
`api/admin/connections.py` imports it directly, and core must not import
plugin code. The `zoom_webhooks` intake Lambda and its API-router wiring
stay core for the same reason.
