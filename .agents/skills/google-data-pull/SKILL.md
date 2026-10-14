---
name: google-data-pull
description: Pull data read-only from Alexey's Gmail (alexey.s.grigoriev@gmail.com and alexey@datatalks.club), Google Calendar, and Google Drive/Docs/Sheets using short-lived access tokens from Dapier's OAuth connections. Covers which connection serves which account, `dapier token exec`, Gmail search/pagination/bodies/attachments, Calendar events in a window, and Drive search plus Docs/Sheets export to text/CSV. Use when asked to read, search, export, or summarize mail, calendar events, or Drive files.
---

# Pull Google data through Dapier (read-only)

Dapier holds the Google OAuth connections. The refresh tokens and the OAuth
client secret never leave Dapier. Agents ask the API for a short-lived
access token and pass it to a child process. `gpull.py`, next to this file, is
a stdlib-only client that makes only GET requests.

Run everything from the dapier repo root. The `dapier` CLI is not on PATH, so
call it through `uv run` (see the `dapier-cli` skill for auth and 401s).

## Where credentials live

| What | Where | You touch it? |
|---|---|---|
| Refresh token, access token, client override | Dapier DynamoDB credential record `oauth#<connection-id>` | Never. Only the API reads it |
| Shared Google OAuth client | Dapier `oauth-clients` (`uv run dapier oauth-clients --help`) | No |
| Your DTC session that authorizes token requests | `~/.config/dapier/session.json` (owner-only) | Through `uv run dapier auth` only |
| Access token handed to you | `DAPIER_ACCESS_TOKEN` in the child process environment | Read by `gpull.py`; never print it |

## Which connection serves which account

All three are granted to the `admin-os` agent for Alexey's DTC identity
(`uv run dapier grants list`).

| Account | Connection id | Address it as | Read scopes |
|---|---|---|---|
| alexey.s.grigoriev@gmail.com | `google-calendar` | `gmail alexey.s.grigoriev`, `calendar alexey.s.grigoriev`, `drive alexey.s.grigoriev` | gmail.readonly, drive.readonly, spreadsheets, calendar.events.owned, calendar.freebusy |
| alexey@datatalks.club (mail) | `google-gmail-datatalks` | `gmail datatalks` | gmail.readonly |
| alexey@datatalks.club (Drive/Docs/Sheets) | `google-sheets` | `drive datatalks` | drive.readonly, spreadsheets |

The DataTalks account has no Calendar connection. The personal connection
uses one token for Gmail, Calendar, and Drive. Its connection id is
`google-calendar` for historical reasons.

Check the current state before you rely on this table:

```bash
uv run dapier connections list            # account, services, health
uv run dapier connections show google-calendar   # scopes, expiry (no secrets)
uv run dapier connections test google-gmail-datatalks
```

## Getting a token

```bash
uv run dapier token exec <service> <account> --agent admin-os -- <command...>
```

The API returns a fresh access token after it checks that the provider
account matches the connection's verified account. The CLI sets
`DAPIER_ACCESS_TOKEN` only in the child process. The token never appears in
argv, the parent shell, or the output. Confirm the account and scopes:

```bash
G=.agents/skills/google-data-pull/gpull.py
uv run dapier token exec gmail datatalks --agent admin-os -- python3 $G whoami
# {"email": "alexey@datatalks.club", "expires_in": 3598, "scopes": [...]}
```

`uv run dapier token write <service> <account> --agent admin-os --output FILE`
writes the token to a 0600 file for tools that cannot read the environment.
Prefer `exec`. If you use `write`, delete the file when you finish and keep it
out of the repo.

### Refresh handling

- Dapier refreshes the token server-side. A request refreshes the token if
  the stored access token has expired or Google rejects it. Then Dapier
  re-verifies the account binding. You never handle a refresh token.
- A token can arrive with only a few minutes left. Dapier refreshes on expiry,
  not ahead of it. `whoami` reports `expires_in`. Start one `token exec` for
  each command or batch. For a long job, run `token exec` again for each chunk.
  Do not cache a token.
- An HTTP 401 from Google mid-run means the token expired. Run the command
  again under a new `token exec`.
- If `token exec` fails with "no verified provider account", "revoked",
  or a binding mismatch, the connection needs operator consent. Refresh it with
  the `dapier-refresh-connections` skill or
  `uv run dapier connections connect <id> --agent admin-os`. Do not work
  around it.

## Gmail

```bash
G=.agents/skills/google-data-pull/gpull.py
T="uv run dapier token exec"

# 5 newest messages (id, thread_id, subject, from, to, date, labels, snippet), JSONL
$T gmail datatalks --agent admin-os -- python3 $G gmail list --max 5
$T gmail alexey.s.grigoriev --agent admin-os -- python3 $G gmail list --max 5

# Gmail search syntax (same as the Gmail search box)
$T gmail datatalks --agent admin-os -- python3 $G gmail list --q "from:stripe.com newer_than:30d"
$T gmail datatalks --agent admin-os -- python3 $G gmail list --q 'subject:(invoice OR receipt) after:2026/09/01 before:2026/10/01'
$T gmail alexey.s.grigoriev --agent admin-os -- python3 $G gmail list --q "has:attachment filename:pdf newer_than:60d"
$T gmail datatalks --agent admin-os -- python3 $G gmail list --q "in:sent to:someone@example.com" --max 10
$T gmail datatalks --agent admin-os -- python3 $G gmail list --label INBOX --label UNREAD

# Pagination: --max N follows nextPageToken until N messages; --max 0 = everything.
# --ids-only skips the per-message metadata request (much faster for large sweeps).
$T gmail datatalks --agent admin-os -- python3 $G gmail list --q "newer_than:30d" --ids-only --max 0 > ids.jsonl

# Bodies: decoded text/plain (falls back to tag-stripped HTML) + attachment list
$T gmail datatalks --agent admin-os -- python3 $G gmail get <message-id>
$T gmail datatalks --agent admin-os -- python3 $G gmail get <message-id> --out .tmp/google-pull/msg.json
$T gmail datatalks --agent admin-os -- python3 $G gmail thread <thread-id>   # whole conversation
$T gmail datatalks --agent admin-os -- python3 $G gmail raw <message-id> --out .tmp/google-pull/msg.eml
$T gmail alexey.s.grigoriev --agent admin-os -- python3 $G gmail attachment <message-id> <attachment-id> --out .tmp/google-pull/file.pdf
$T gmail datatalks --agent admin-os -- python3 $G gmail labels
```

`list` makes one request per message for metadata. For thousands of
messages, use `--ids-only` and fetch only the messages you need.

## Calendar (alexey.s.grigoriev@gmail.com)

```bash
# Next 5 events from now on the primary calendar (recurring events expanded, ordered by start)
$T calendar alexey.s.grigoriev --agent admin-os -- python3 $G calendar events --start "$(date -u +%Y-%m-%dT%H:%M:%SZ)" --max 5

# A window: dates mean 00:00 UTC; --end is exclusive; --max 0 = every page
$T calendar alexey.s.grigoriev --agent admin-os -- python3 $G calendar events --start 2026-10-01 --end 2026-10-15 --max 0
$T calendar alexey.s.grigoriev --agent admin-os -- python3 $G calendar events --start 2026-10-14T00:00:00+02:00 --end 2026-10-21T00:00:00+02:00 --max 0
$T calendar alexey.s.grigoriev --agent admin-os -- python3 $G calendar events --q "podcast" --start 2026-10-01
```

`calendar calendars` (the calendar list) currently fails with **HTTP 403
insufficient authentication scopes**. The `google-calendar` connection has
`calendar.events.owned` and `calendar.freebusy` but not
`calendar.readonly`. The Google plugin README says it should have that scope.
`events` works on calendars the account owns, including `primary`. To read
other calendars or the calendar list, an operator must add the scope and then
give consent again. Keep the existing scopes, because `--scopes` sets the full
list:

```bash
uv run dapier connections edit google-calendar --scopes \
  https://www.googleapis.com/auth/calendar.events.owned \
  https://www.googleapis.com/auth/calendar.freebusy \
  https://www.googleapis.com/auth/calendar.readonly \
  https://www.googleapis.com/auth/documents \
  https://www.googleapis.com/auth/drive.readonly \
  https://www.googleapis.com/auth/gmail.readonly \
  https://www.googleapis.com/auth/gmail.send \
  https://www.googleapis.com/auth/spreadsheets \
  https://www.googleapis.com/auth/userinfo.email openid
uv run dapier connections connect google-calendar --agent admin-os   # browser consent as alexey.s.grigoriev@gmail.com
```

## Drive, Docs, Sheets

```bash
# Name search, newest first: id, name, mime, modified, size, owners, link
$T drive datatalks --agent admin-os -- python3 $G drive search --name newsletter --max 10
$T drive alexey.s.grigoriev --agent admin-os -- python3 $G drive search --name newsletter

# Content search, type filter, shared drives, raw Drive query clauses (ANDed)
$T drive datatalks --agent admin-os -- python3 $G drive search --fulltext newsletter --mime application/vnd.google-apps.document
$T drive datatalks --agent admin-os -- python3 $G drive search --name newsletter --all-drives
$T drive datatalks --agent admin-os -- python3 $G drive search --q "modifiedTime > '2026-09-01T00:00:00'" --max 0

# Export: Google Doc -> text/plain, Sheet -> CSV (FIRST TAB ONLY), Slides -> text;
# non-Google files (PDF, images, ...) are downloaded as-is.
$T drive datatalks --agent admin-os -- python3 $G drive export <doc-id> --out .tmp/google-pull/doc.txt
$T drive datatalks --agent admin-os -- python3 $G drive export <doc-id> --mime text/markdown --out .tmp/google-pull/doc.md
$T drive alexey.s.grigoriev --agent admin-os -- python3 $G drive export <sheet-id> --out .tmp/google-pull/sheet.csv
$T drive alexey.s.grigoriev --agent admin-os -- python3 $G drive export <pdf-id> --out .tmp/google-pull/file.pdf
$T drive datatalks --agent admin-os -- python3 $G drive meta <file-id>

# Every tab of a spreadsheet as CSV (Sheets API), or one A1 range
$T drive alexey.s.grigoriev --agent admin-os -- python3 $G drive sheet <sheet-id> --out-dir .tmp/google-pull/sheet
$T drive datatalks --agent admin-os -- python3 $G drive sheet <sheet-id> --range "Sheet1!A1:D50"
```

Use the Drive connection of an account that can see the file. Many DataTalks
files are shared with both accounts. If one connection returns 404 for a
file, try the other one.

## Output conventions

- Listings print one JSON object per line (JSONL) to stdout. Pipe them to `jq`
  or `python3 -c`, or redirect them to a file.
- Files go to the `--out` path. The written path is printed on stderr, and
  `--out -` (the default for `export`/`raw`) writes to stdout.
- Put pulled data under `.tmp/google-pull/` (gitignored) or a scratch
  directory outside the repo. Never commit pulled mail, events, or documents.
- Errors exit non-zero with `HTTP <code> for <endpoint>: <Google message>`.
  The output never includes tokens.

## Read-only safety rules

- Only read. Do not send, draft, label, archive, trash, or delete mail. Do not
  create or edit events. Do not create, edit, share, or delete Drive files.
  Some connections also have write scopes (`gmail.send`, `documents`,
  `spreadsheets`, `calendar.events.owned`) that Dapier flows use. Do not use
  them here.
- `gpull.py` sends only GET requests to Google APIs (and reads token metadata
  from the tokeninfo endpoint). Its request helper refuses other methods.
  Keep that guard when you extend it, and do not call Google write endpoints
  with curl as a workaround.
- Never print, log, or write `DAPIER_ACCESS_TOKEN` or any token. Do not pass a
  token on a command line. Refer to credentials only by location.
- Do not add scopes, create connections, or change grants to finish a pull.
  Report what the operator needs to do (see the Calendar scope gap above).
- Report counts and the minimum content needed. Mail, calendar, and Drive
  content is private.
