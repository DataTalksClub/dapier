# Changelog

## Unreleased

New operator CLI nouns `dapier gmail` and `dapier gdrive` over the new
operator-gated `/api/agent/gmail/*` and `/api/agent/gdrive/*` routes.
Every operation names its Google account explicitly (`--account
email-or-connection-id`); multiple accounts require the choice and an
ambiguous selection is rejected with the candidates — never a silent
default. Gmail: search/list/read messages, threads, labels, attachments,
drafts create/update/delete with attachments, direct send, replies as
direct send or reply draft (In-Reply-To/References/subject/thread
preserved), send existing draft, mark read/unread/archive/trash/label.
Drive: search/list/meta, download/export (Google-native docs to
PDF/CSV/PNG, `--export-mime` override), upload (file or stdin), update,
copy, move, share, trash (recoverable by default), restore, mkdir. New `dapier
calendar` noun over `/api/agent/calendar/*`: calendar list, events
list/search in bounded windows (≤ 366 days), free/busy, event
create/update/delete with attendees, recurrence (RRULE), timezones,
reminder overrides and attachment links — guest email is explicit
(`sendUpdates` defaults to `none`, so invitations never go out by
accident) and reviewed JSON event payloads can be applied verbatim.
Every write verb on the three nouns accepts `--preview`, printing the
exact request body (attachments as sizes) without sending. MIME construction
reuses the engine's raw builder (Unicode bodies, binary attachments); files
move as base64 through the JSON API; connection tokens stay server-side;
granted scopes are honored as-is — insufficient grants fail with the
provider's 403 plus a scope-fix hint.

YouTube push notifications and backup polls now share step execution identities
by video ID, preventing duplicate announcements across the two sources. Failed
steps still retry and explicit operator replays still run again.

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
