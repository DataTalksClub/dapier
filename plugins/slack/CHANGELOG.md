# Changelog

## 1.0.0 — 2026-10-03

First release as a plugin. No behavior change: the connector's actions
(send/schedule/DM/update/react/pin/find/invite/channel/topic/purpose/upload),
the trigger chip, the Events-API trigger-sample discovery, channel/user
connection discovery, the auth.test health check, and the
`slack.messages` poll source all registered exactly as before — now from
`plugins/slack/` with the plugin manifest instead of the hand-maintained
core connector lists.
