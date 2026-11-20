# Changelog

## 1.2.0 — 2026-11-20

`telegram_format` mirrors a post's attachment instead of posting "no text
in this post, see Telegram for the attachment" (t.me/aidevtoolszoomcamp/81:
a caption-less photo reached #course-ai-dev-tools-zoomcamp as that
placeholder). The trigger's own bot (`telegram_connection_id`, else the
connection of the event's telegram hook) downloads the file via `getFile`.
A photo, or an image sent as a file, is uploaded to Slack and shown as an
image block in the channel post under its caption. When the upload is
refused (the token lacks `files:write`), the public t.me preview image is
shown instead. Other files are uploaded into the post's thread. A post with
no text and nothing to show is skipped, never posted as a placeholder. The
output gains `media` (`{kind, via}`), `media_error` and `skipped`.

## 1.1.1 — 2026-10-31

The `slack.messages` poll source now resolves the stored OAuth user token
(`access_token`) as well as a pasted bot token, exactly like the actions'
runner — an OAuth-only connection no longer fails the poll with "has no
stored bot token". The Events-API intake URL (`/hooks/slack/{connection_id}`)
became reachable: API Gateway now routes POSTs to it (intake 9d9e0a43).

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
