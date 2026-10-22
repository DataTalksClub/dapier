# Changelog

## 1.1.0 — 2026-10-22

Slack connects through OAuth user-scope consent (`dapier connections
create --provider slack` + `connect`): the authorize URL carries
`user_scope=im:history,im:write,users:read,search:read`, the code exchange
stores the `authed_user.access_token` (`xoxp-`), and issuance verifies
against `auth.test` with no refresh grant. Pasted bot/user tokens still
import and verify exactly as before (the Au-Tomator bot connection is
unchanged).

## 1.0.0 — 2026-10-03

First release as a plugin. No behavior change: the connector's actions
(send/schedule/DM/update/react/pin/find/invite/channel/topic/purpose/upload),
the trigger chip, the Events-API trigger-sample discovery, channel/user
connection discovery, the auth.test health check, and the
`slack.messages` poll source all registered exactly as before — now from
`plugins/slack/` with the plugin manifest instead of the hand-maintained
core connector lists.
