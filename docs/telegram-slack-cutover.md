# Telegram → Slack cutover (au-tomator port)

The old `au-tomator-telegram-bot` (long-polling process on the bot host,
token in its `.env`) reposts DTC announcement-channel posts to Slack course
channels. Dapier now does the same job: a Telegram hook trigger bound to the
bundled `telegram-to-slack` flow (workflows/telegram-slack.yaml). Rendering
lives in the `slack` action's `telegram_format` mode; the chat → channel
mapping is the flow's condition chain.

## What the port preserves

- Only posts in mapped channels are forwarded (unmatched chats are skipped).
- Telegram entities (bold, italic, links, inline code, fenced blocks) render
  as Slack mrkdwn; prose is quoted, code stays code.
- Posts longer than one Slack message continue in a thread, and the thread
  closes with a link to the original post (`source_link` per channel).

## Cutover steps

1. **Deploy first** — the flow must be in the deployed bundle before the
   trigger can bind to it (`git push` to `main` runs the deploy pipeline).

2. **Stop the old bot** (Telegram serves either polling or a webhook, never
   both; registering the webhook while the poller runs 409-conflicts it):
   stop the bot-master-managed `au-tomator-telegram-bot` process and disable
   its autostart.

3. **Import the bot token as a connection** (operator token providers are
   verified via `getMe` and marked connected):

   ```
   cp /home/alexey/bots/au-tomator-telegram-bot/.env /tmp/tgenv   # not committed
   dapier connections import automator-telegram --provider telegram \
       --token-file /tmp/tgenv-only-the-token --display-name "automator-telegram"
   ```

4. **Save the trigger** (this registers the Telegram webhook with the
   per-trigger secret):

   ```
   dapier hooks save - <<'JSON'
   {"name": "automator-telegram", "kind": "telegram",
    "connection_id": "automator-telegram", "flow": "telegram-to-slack",
    "description": "Repost DTC announcement channels to Slack (au-tomator port)"}
   JSON
   ```

5. **Test** by posting into https://t.me/dtc_test (mapped to the
   `integration_test` Slack channel) and checking the thread got the t.me
   link. Then post to a real channel to confirm.

## Adding a channel

One condition branch in `workflows/telegram-slack.yaml`
(`chat_id` → `channel` + `source_link`), then deploy. The trigger needs no
re-save: stored triggers resolve the flow by name at run time.

## Rollback

Stop forwarding with `dapier hooks delete automator-telegram --kind telegram`
(this also releases the bot's webhook), then restart the old poller. The old
bot's code is untouched at /home/alexey/bots/au-tomator-telegram-bot.
