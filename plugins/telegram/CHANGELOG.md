# Changelog

## 1.0.0 — 2026-10-03

First release as a plugin. No behavior change: the send/photo/document/poll
actions, find-chat and the pin/ban/unban maintenance actions, chat discovery,
the getMe health check, the trigger chip, and the per-event sample pull
registered exactly as before — now from `plugins/telegram/` with the plugin
manifest instead of the hand-maintained core connector lists.

`telegram_api.py` (the Bot API helper the core router and hook triggers use)
and `telegram_format.py` (Telegram-entity → Slack-blocks rendering, shared
with the slack plugin's runner) stay in core — core must not import plugin
code, and the renderer serves two integrations.
