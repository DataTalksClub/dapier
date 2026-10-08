# Connections page — design contract

This page is a **service switchboard**, not an OAuth-grant spreadsheet.
The family shell, type, tokens, and banned tells in
[`design-contract.md`](design-contract.md) still apply. This file pins the
Connections-only decisions.

## Attack (what was wrong)

Verified against the live `/connections` register (2026-10-05):

1. **Grouped by OAuth provider.** Every Google product sat under one
   "GOOGLE" header, so Calendar, Drive, Gmail, Docs, and Sheets were one
   soup. The operator could not tell which service a row was for.
2. **Mashed titles.** Rows were named "Google Calendar + Drive (Gmail)"
   and "Google Drive, Docs & Sheets (DataTalks)" — a display-name pun on
   the scope list, not an identity.
3. **Status jammed into expiry.** `connected` and `token expires …` sat
   on one inline line (`connectedtoken expires`) because `.cell-sub` only
   stacks inside `.cell-title`.
4. **Connect picker hid the family.** Adding a Google account offered
   Calendar and YouTube. Drive, Docs, Sheets, and Gmail were missing, so
   people piled scopes onto the Calendar connection.
5. **Spreadsheet chrome.** One table, uppercase group labels, three
   equal columns, two equal-weight row buttons. No tension: the service
   was not the heading, the account was not the identity.
6. **Credential sharing was invisible.** One Google refresh token already
   backs several products; the UI never said so, so it looked like three
   unrelated Google rows.

## Direction

A vertical **register of services**. Every service — Gmail, Calendar,
Drive, Docs, Sheets, YouTube, Dropbox, Slack, Telegram, Zoom — gets one
panel; a Google grant covering several products appears under each of
them, with a "Same grant as …" line so the shared credential stays
visible. Rows within a panel sort by verified account.

Under the hood the Google products on one email still share one connection
record and one refresh token. Adding Gmail to an account that already has
Drive merges Gmail scopes onto that record and reconnects; it does not
mint a second Google credential. An unfinished grant with no verified
email is "Not signed in", never a fake Drive account.

## Pinned treatments

- **Headings** are the service name (Gmail, Google Calendar, YouTube,
  Dropbox…), one panel per service. Google G / product mark only in the
  panel header — not on every row.
- **Row identity** is the verified account (email, channel, workspace).
  Never the mashed display name ("Google Calendar + Drive (Gmail)").
  Unverified Google grants title **Not signed in**. The connection id is
  mono, secondary. Display names stay in Manage as a nickname.
- **Same-grant line** on rows whose grant covers other products:
  `Same grant as Calendar, Drive · google-calendar`. Sentence case,
  muted, not a chip.
- **Status** is a stacked dot+word; expiry is a second line. Never inline
  with the word "connected". A token still valid but inside the 48h digest
  horizon reads **expiring soon** (warning) with `expires …` underneath;
  a lapsed token reads **needs reconnection**. Both count as needing
  attention, including the Connected / Needs attention filter.
- **Actions:** Manage on every row. Finish setup and Reconnect are
  secondary. One primary on the page (Add connection). Reconnect is on
  every OAuth row that needs it (Google, YouTube, Dropbox, Zoom) —
  already lapsed, or still connected but expiring soon. Slack and
  Telegram paste a new token in Manage. Provider access tokens are
  CLI-only (`dapier token exec|write`); the console does not reveal them.
  Expiry reminder email is the daily ConnectionDigestFunction schedule,
  not a send-now button on this page.
- **Add picker** lists services, not OAuth providers. Google products are
  first-class cards with their own default scopes.
- **Empty / filter empty** stay the family empty-state, no illustration.
- **Panels** reuse `.data-panel`. No new card grid, no pastel chips, no
  eyebrow labels, no icons inside labelled buttons.

## Banned on this page

Provider-grouped "GOOGLE" headers; mashed multi-product titles as the
row name; an unfinished `google-drive` row pretending to be a Drive
account (it reads "Not signed in"); inline status+expiry; a "Tokens
expiring soon" banner that repeats mashed display names above the
register; a send-now "Email re-auth reminder" button; a connect catalog
that only offers "Google Calendar" and "YouTube" for the whole Google
family; pastel service pills; an icon on every account row.

## Surfaces

The API's public connection view carries `services` (id + label), derived
from granted (else requested) scopes. The CLI list prints a SERVICES
column and an ACCOUNT column. The console renders one panel per service
for every provider; a multi-product Google grant repeats under each product
with a same-grant line. All three read the catalog in
`src/dapier/connections/services.py`; the console duplicates ids/scopes
and a test pins them together.

Adding a Google product to an existing grant is `connections edit
<id> --scopes …` then `connections connect <id>` — the same PUT +
consent the console reuse dialog runs. New accounts still use
`connections create` with `--provider google` (or `youtube`) and the
product's default scopes.
