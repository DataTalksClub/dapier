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

A vertical **register of services**. Gmail, Calendar, Drive, Docs, Sheets,
and YouTube each own a family panel (muted header band, hairline rows).
Dropbox, Slack, Telegram, and Zoom sit in the same rhythm. An account
that grants several Google products appears under each product and names
the shared grant in a quiet line.

Under the hood the Google products still share one connection record and
one refresh token. Adding Gmail to an account that already has Drive
merges Gmail scopes onto that record and reconnects; it does not mint a
second Google credential.

## Pinned treatments

- **Section heading** is the service name (Gmail, Calendar, Drive…),
  Google G / product mark only in the panel header — not on every row.
- **Row identity** is the verified account (email, channel, workspace).
  The connection id is mono, secondary. Display names stay in Manage.
- **Shared grant** copy: `Same grant as Drive, Sheets · google-sheets`.
  Sentence case, muted, not a chip.
- **Status** is a stacked dot+word; expiry is a second line. Never inline
  with the word "connected".
- **Actions:** Manage + Get token stay (family departure already
  recorded). Finish setup and Reconnect are secondary. One primary on
  the page (Add connection). Reconnect is on every OAuth row that needs
  it (Google, YouTube, Dropbox, Zoom). Slack and Telegram paste a new
  token in Manage.
- **Add picker** lists services, not OAuth providers. Google products are
  first-class cards with their own default scopes.
- **Empty / filter empty** stay the family empty-state, no illustration.
- **Panels** reuse `.data-panel`. No new card grid, no pastel chips, no
  eyebrow labels, no icons inside labelled buttons.

## Banned on this page

Provider-grouped "GOOGLE" headers; mashed multi-product titles as the
row name; inline status+expiry; a connect catalog that only offers
"Google Calendar" and "YouTube" for the whole Google family; pastel
service pills; an icon on every account row.

## Surfaces

The API's public connection view carries `services` (id + label), derived
from granted (else requested) scopes. The CLI list prints a SERVICES
column. The console groups by that list. All three read the catalog in
`src/dapier/connections/services.py`; the console duplicates ids/scopes
and a test pins them together.

Adding a Google product to an existing grant is `connections edit
<id> --scopes …` then `connections connect <id>` — the same PUT +
consent the console reuse dialog runs. New accounts still use
`connections create` with `--provider google` (or `youtube`) and the
product's default scopes.
