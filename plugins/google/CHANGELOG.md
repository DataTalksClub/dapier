# Changelog

## 1.0.0 — 2026-10-03

First release as a plugin. No behavior change: the Sheets, Drive, Calendar,
Gmail and YouTube actions, their discovery resources and trigger samples,
the Google and YouTube identity health checks, the five trigger chips, and
the eight poll sources register exactly as before — now from
`plugins/google/` with the plugin manifest instead of the hard-coded
registry maps and `_BUILTIN_MODULES` list.

Flow-owned glue stays in core: the Gmail scope policy
(`connection_grant_scopes`/`GMAIL_SCOPES`) lives in
`connections/providers/oauth_providers.py` — the OAuth flow and the agent
API call it, and core must not import plugin code. The same rule keeps the
`youtube_subscriptions` intake Lambda (WebSub renewals) in
`triggers/intake/`.
