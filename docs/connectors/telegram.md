# Telegram connector

Telegram bots authenticate with a token straight from @BotFather — no OAuth
consent round-trip. Dapier stores the token on a connection, posts through
the Bot API with it, and drives triggers by registering the bot's single
webhook with Telegram.

## Connect it

1. In Telegram, message [@BotFather](https://t.me/BotFather) and create a
   bot (`/newbot`). BotFather issues a token shaped like
   `123456:ABC-DEF…` (digits, a colon, a secret); Dapier validates that
   shape before storing anything.
2. Store it on a connection. In the console use **Connectors → Telegram →
   Create new** and paste the token; with the CLI, import it from a file:

   ```sh
   uv run dapier connections import telegram-bot --provider telegram \
       --token-file /path/to/telegram-token
   ```

   Use `--display-name` to set its label. The token is stored write-only.
3. Verify it: `uv run dapier connections test telegram-bot` calls the Bot
   API's `getMe` and answers with the bot's identity. The console's
   connection test button runs the same check.

To replace a token, edit the connection or import a new one. See the
[shared connector guide](README.md#cli-parity-grants-and-lifecycle) for the
common connection commands.

## Triggers: the setWebhook lifecycle

A Telegram trigger binds one bot connection to a delivery URL:

```
https://<dapier-host>/hooks/telegram/<trigger-name>
```

Save it from the console's **Triggers** view (kind **Telegram**, the bot
connection) or the CLI:

```sh
uv run dapier hooks save telegram-hook.json
```

```json
{
  "name": "bot-inbox",
  "kind": "telegram",
  "connection_id": "telegram-bot",
  "actions": [
    {"type": "telegram_send", "connection_id": "telegram-bot",
     "text": "You said: {text}"}
  ]
}
```

On every enabled save Dapier calls the Bot API's `setWebhook`, pointing the
bot at the trigger URL with a per-trigger secret. Telegram echoes that
secret in `X-Telegram-Bot-Api-Secret-Token` on every update, and Dapier
verifies it before anything runs — no shared infrastructure to configure.
The stored trigger re-registers on every save, so replacing the bot's token
and re-saving heals the registration. Disabling an existing trigger releases
the bot's webhook; deleting the trigger unregisters it best-effort.

Lifecycle rules worth knowing:

- **One webhook per bot.** Telegram keeps a single webhook per token, so one
  connection can drive at most one enabled trigger. A second save on the
  same connection fails, naming the trigger that already owns the bot.
- **Fixed fast ack.** Telegram retries any update Telegram did not answer
  quickly with 200, so Telegram triggers must not configure the `response`
  object (that is a webhook-trigger-only feature) — Dapier rejects it and
  answers a fixed ack itself.
- The bot sees private and group messages it is a participant in, plus
  channel posts (`setWebhook` includes them) — put the bot in the group or
  announcement channel that should drive workflows.

### Events

One stored trigger matches three events, so a workflow can select by event
name instead of poking at the raw update:

| Event | Fires on | Envelope `data` fields |
| --- | --- | --- |
| `message.received` | `message` / `edited_message` updates | `hook`, `update_id`, `message_id`, `text` (or a media caption), `entities`, `is_channel_post`, `chat_id`, `chat`, `from`, `update` |
| `channel_post.received` | `channel_post` / `edited_channel_post` (announcement channels; no `from` — an `author_signature` instead) | same fields |
| `callback_query.received` | a user taps an inline-keyboard button | `hook`, `update_id`, `id` (callback id), `data` (the button's `callback_data`, e.g. `join:september`), `from` (the tapper), `inline_message_id` (keyboards on inline-mode messages only), plus the originating message's `message_id`, `text`, `chat_id`, `chat` when present |

Button taps need the bot to be subscribed to them: new trigger
registrations include `callback_query` in `set_webhook`'s
`allowed_updates`; an existing Telegram trigger must be re-saved once to
re-register. A sample pull,
`dapier triggers sample --connector telegram --event callback_query.received`,
returns the documented tap. Creating the buttons themselves still means
sending a `reply_markup` inline keyboard through the API — the send actions
have no `reply_markup` field yet.

## Discovery and the getUpdates conflict

Telegram connections expose three live listings to the console/CLI pickers
and `dapier connections discover telegram-bot [resource]`: `chats` (chats
seen in the bot's pending updates, deduplicated), `updates` (the pending
updates with their chats), and `chat` (one chat's profile, needs a
`chat_id` param — an `@name` or a `-100…` id).

The catch: Telegram serves `getUpdates` — the call behind `chats` and
`updates` — only while **no webhook is set**, and the webhook is exactly
what Dapier's triggers use. A live bot therefore gets Telegram's own
conflict message back (HTTP 502) instead of a listing. That is the normal
working state of a dapier telegram trigger, not a breakage. In practice:

- Discover chats before enabling the trigger, or
- address chats explicitly: `@channelname` or the `-100…` id works in
  `telegram_find_chat` and every send action, and `find_chat` verifies a
  chat is visible to the bot.

## Actions

All Telegram actions take `connection_id`. Send targets fall back to the
triggering chat: leave `chat_id` empty and a Telegram-triggered run replies
in place (`{data.chat_id}`); any other trigger must name the chat.

| action | Bot API call | notes |
| --- | --- | --- |
| `telegram_send` | `sendMessage` | templated `text`; `chat_id` defaults to the triggering chat; output `{message_id, chat_id}` |
| `telegram_find_chat` | `getChat` | one chat's profile; a chat the bot cannot see is `{found: false}`, not an error |
| `telegram_send_photo` | `sendPhoto` | media from exactly one of `source_url` (dapier downloads it, so it need not be reachable by Telegram) or staged `source_s3 {bucket, key}`; JPEG/PNG/GIF/WEBP under 10 MB; optional `caption`, `filename`; output `{message_id, chat_id}` |
| `telegram_send_document` | `sendDocument` | same media sourcing; any file type up to 50 MB; the filename rides along |
| `telegram_send_poll` | `sendPoll` | `question` plus `options` one per line (2–10 after trimming empty lines); `anonymous` defaults to true; output `{message_id, chat_id, poll: {id, question}}` |

`telegram_send_photo` and `telegram_send_document` accept the staged
`source_s3` output of `dropbox_read_file`, `drive_read_file`, or
`s3_read_object` — bucket/key take templates.

## Gotchas

- One bot, one trigger: enable a second trigger only on another bot's
  connection, or disable the first to release the webhook.
- While the trigger is enabled, `telegram.chats` / `telegram.updates`
  discovery fails with Telegram's webhook-conflict message. Sample
  discovery of the trigger chip knows this: it falls back to the newest
  recorded run, then a documented example.
- `chat_id` fallback only works under a Telegram trigger; from any other
  trigger (email, schedule, webhook) the send needs an explicit `chat_id`.
- Photo sends fail for formats or sizes outside JPEG/PNG/GIF/WEBP and
  10 MB; documents accept anything up to 50 MB.
- Polls need 2–10 non-empty option lines; extra blank lines are fine, a
  single option is not.
