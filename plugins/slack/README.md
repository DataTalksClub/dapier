# Slack connector

Slack does not use Dapier's OAuth client. Paste a Slack bot or user token;
Dapier validates it with `auth.test` before storing it write-only under the
connection's credential ID.

1. Create or select an app at [api.slack.com/apps](https://api.slack.com/apps).
2. Under **OAuth & Permissions**, grant the bot at least `chat:write`; add
   `channels:read` or `groups:read` if the workflow needs to inspect those
   channels, and `search:read` if it uses the Find Message action
   (`slack_find_message`).
3. Install the app to the workspace and copy the **Bot User OAuth Token**
   (`xoxb-…`). A user token (`xoxp-…`) also works.
4. Invite the bot to channels where it should post, for example
   `/invite @your-bot`.
5. In Dapier, open **Connectors → Slack → Create new**, paste the token, and
   save. The connection should show **connected** with the workspace identity.

A bad or revoked token is rejected before it is stored. Slack scopes are
carried by the token, so the requested-scopes editor is hidden for Slack.
Replace a token by editing the connection or importing a new token using the
CLI. See the [shared connector guide](README.md#cli-parity-grants-and-lifecycle)
for the common connection commands.

## Event triggers: signing secret and Request URL

Slack triggers use the same connection, plus two more pieces of app setup:
the signing secret (to verify deliveries) and a Request URL (where Slack
delivers them). Each connection gets its own Request URL, so one Slack app
can serve one workspace per connection:

```
https://<dapier-host>/hooks/slack/<connection-id>
```

The `dapier connections import` command prints this URL; the console's
Manage-connection dialog shows it too.

No Slack app at all? The `slack.messages` poll source reads one channel's
history on a schedule and publishes the same `message.received` event —
create a poll trigger with source `slack.messages`, a `connection_id`, and
the `channel_id` to watch (reactions, mentions and joins still need the
Events API).

1. Copy the **Signing Secret** from the Slack app's **Basic Information →
   App Credentials** page and store it on the connection before touching
   the Request URL — Slack signs even the URL-verification handshake, so
   the validation click in step 3 fails without it. Paste it in the
   console's Manage-connection dialog, or import it from a file:

   ```sh
   uv run dapier connections import <connection-id> --provider slack \
       --token-file /path/to/slack-token \
       --signing-secret-file /path/to/slack-signing-secret
   ```

   `--signing-secret-file` applies only to Slack connections. A token-only
   edit keeps the stored signing secret; passing a new one rotates it
   (16–256 characters).
2. In the Slack app settings, open **Event Subscriptions**, switch it on,
   and paste the connection's Request URL. Slack verifies the URL by
   sending a signed `url_verification` challenge, which dapier answers
   automatically.
3. Under **Subscribe to bot events**, add the Slack event types you want:
   `message.channels`, `message.groups`, `message.im`, `message.mpim`,
   `app_mention`, `reaction_added`, `member_joined_channel`.

Dapier verifies every delivery the way Slack signs it:
`v0=<hmac-sha256(signing secret, "v0:<timestamp>:<raw body>")>` echoed in
`X-Slack-Signature` with `X-Slack-Request-Timestamp`. Unsigned deliveries
and deliveries older than five minutes from their signed timestamp are
rejected with 401. Redeliveries of the same Slack event deduplicate on
Slack's `event_id`.

### Supported events

| Slack event type | Dapier event | Envelope highlights |
| --- | --- | --- |
| `message.channels`, `message.groups`, `message.im`, `message.mpim` (incl. edit subtypes) | `message.received` | `channel_id`, `user_id`, `text`, `ts`, `thread_ts`, `subtype` |
| `app_mention` | `app.mention` | same message fields; `text` names the bot, e.g. `<@U0…>` |
| `reaction_added` | `reaction.added` | `channel_id` (from `item`), `user_id` (who reacted), `reaction`, `ts` (when), `item_ts` + `item_user` (reacted-to message) |
| `member_joined_channel` | `member.joined` | `channel_id`, `user_id`, `inviter` |

Every envelope also carries `connection_id`, `team_id`, `event_id`,
`event_time`, `type`, and the raw Slack `event` for anything beyond the
flattened fields. Posts made by apps (`bot_id` present) never publish, so
a workflow posting into the channel it listens on cannot loop. Anything
else Slack sends answers `accepted: false` so Slack stops retrying it.

Note on compatibility: `app_mention` used to publish `message.received`
like the message family; it now publishes `app.mention` only. Workflows
that relied on mentions firing `message.received` should re-filter on
`app.mention` — `message.received` itself is unchanged.

## Actions

All Slack actions take `connection_id` (or a bare `credential_id`) and
render text fields from the event. Channel and message fields of the
maintenance actions prefill from a Slack trigger envelope: `{channel_id}`
and `{ts}`.

| action | Slack call | notes |
| --- | --- | --- |
| `slack` | `chat.postMessage` | templated message to a channel; `telegram_format` renders Telegram updates as blocks; output `{ok, channel, ts}` |
| `slack_dm` | `conversations.open` + `chat.postMessage` | DM one user; chain `slack_find_user` and pass `{steps.<id>.output.user.id}` |
| `slack_update_message` | `chat.update` | edit one posted message; `channel` + `ts` render from the event (a Slack trigger envelope carries `{channel_id}`, `{ts}`) |
| `slack_add_reaction` | `reactions.add` | `reaction` is the emoji name without colons; Slack's `already_reacted` counts as success so retried runs stay green |
| `slack_create_channel` | `conversations.create` | name is normalized to what Slack accepts; an existing name errors — chain `slack_find` first for find-or-create |
| `slack_set_topic` | `conversations.setTopic` | sets one channel's topic |
| `slack_set_purpose` | `conversations.setPurpose` | sets one channel's purpose |
| `slack_invite_to_channel` | `conversations.invite` | invite one or more users into a channel; `users` takes comma-separated member ids (chain `slack_find_user` or use the picker); Slack's `already_in_channel` is absorbed as `invited: false` with the `reason`, so a retried run stays green; output `{invited, channel, users}` |
| `slack_pin_message` | `pins.add` | pin one message; `channel` + `timestamp` render from the event (`{channel_id}`, `{ts}`); output `{pinned, channel, timestamp}` |
| `slack_find_message` | `search.messages` | search workspace messages (the token needs `search:read`); `count` caps results (1-100, default 20); output `{found, messages: [{ts, channel_id, channel_name, user, text, permalink}], count}` — a miss is not an error |
| `slack_upload_file` | `files.getUploadURLExternal` + upload + `files.completeUploadExternal` | send one file to a channel; bytes from `source_url`, staged `source_s3 {bucket, key}` (dropbox/drive read file chain here), or inline `content`; optional title, comment, `thread_ts`; the channel takes a channel id (the picker serves ids); the token needs `files:write`; output `{ok, channel, file: {id, name, title, permalink}}` |
| `slack_find` | `users.lookupByEmail` / `conversations.list` | output `{found, user}` or `{found, channel}`; a miss is not an error. `create_if_missing` covers the channel branch (Zapier's Find or Create Channel): a missed name is created via `conversations.create` (`is_private` honored) and the output reports `created: true`; a user miss stays a miss |
| `slack_find_user` | `users.lookupByEmail` | single-purpose user find by email |
| `slack_schedule_message` | `chat.scheduleMessage` | send for later; `post_at` takes an ISO 8601 datetime (a missing offset reads as UTC) or epoch seconds; `thread_ts` schedules the reply into one thread |
| `slack_add_reminder` | `reminders.add` | set one reminder; `time` takes Slack's natural-language times (`in 20 minutes`, `tomorrow 9am`) or epoch seconds — empty = Slack's default (20 minutes); the reminder belongs to the token's own user; output `{ok, reminder: {id, time, text}}` |


