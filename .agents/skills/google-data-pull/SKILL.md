---
name: google-data-pull
description: Pull data read-only from Alexey's Gmail (alexey.s.grigoriev@gmail.com and alexey@datatalks.club), Google Calendar, and Google Drive/Docs/Sheets using short-lived access tokens from Dapier's OAuth connections. Covers which connection serves which account, in-script token fetch via dtoken.py, Gmail search/pagination/bodies/attachments, Calendar events in a window, and Drive search plus Docs/Sheets export to text/CSV. Use when asked to read, search, export, or summarize mail, calendar events, or Drive files.
---

# Pull Google data through Dapier (read-only)

Dapier holds the Google OAuth connections. The refresh tokens and the OAuth
client secret never leave Dapier. Scripts fetch a short-lived access token
in-script via `dtoken.py` (next to this file) and keep it in a local
variable. `gpull.py`, next to this file, is a stdlib-only client that does
this itself and makes only GET requests.

Run everything from the dapier repo root.

## Where credentials live

| What | Where | You touch it? |
|---|---|---|
| Refresh token, access token, client override | Dapier DynamoDB credential record `oauth#<connection-id>` | Never. Only the API reads it |
| Shared Google OAuth client | Dapier `oauth-clients` (`uv run dapier oauth-clients --help`) | No |
| Your DTC session that authorizes token requests | `~/.config/dapier/session.json` (owner-only) | Through `uv run dapier auth` only |
| Access token handed to you | Local variable from `dtoken.dapier_token()` | Never print, log, or persist it |

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

`gpull.py` fetches the token itself — pass the connection reference and the
agent directly:

```bash
G=.agents/skills/google-data-pull/gpull.py
python3 $G --as "gmail datatalks" --agent admin-os whoami
# {"email": "alexey@datatalks.club", "expires_in": 3598, "scopes": [...]}
```

The API returns a fresh access token after it checks that the provider
account matches the connection's verified account. The token lives only in
a local variable inside the script. It never appears in argv, the
environment, or the output.

Own scripts use the same helper (`dtoken.py`, next to `gpull.py`):

```python
import sys
sys.path.insert(0, ".agents/skills/google-data-pull")
import dtoken
access_token = dtoken.dapier_token("gmail datatalks", "admin-os")
# ... Authorization: Bearer {access_token} ...
del access_token
```

### Refresh handling

- Dapier refreshes the token server-side. A request refreshes the token if
  the stored access token has expired or Google rejects it. Then Dapier
  re-verifies the account binding. You never handle a refresh token.
- A token can arrive with only a few minutes left. Dapier refreshes on expiry,
  not ahead of it. `whoami` reports `expires_in`. Fetch one token per command
  or batch. For a long job, fetch again for each chunk. Do not cache a token.
- An HTTP 401 from Google mid-run means the token expired. Fetch a new token
  and run the command again.
- If the fetch fails with "no verified provider account", "revoked",
  or a binding mismatch, the connection needs operator consent. Refresh it with
  the `dapier-refresh-connections` skill or
  `uv run dapier connections connect <id> --agent admin-os`. Do not work
  around it.

## Gmail

```bash
G=.agents/skills/google-data-pull/gpull.py
PULL="python3 $G --agent admin-os --as"

# 5 newest messages (id, thread_id, subject, from, to, date, labels, snippet), JSONL
$PULL "gmail datatalks" gmail list --max 5
$PULL "gmail alexey.s.grigoriev" gmail list --max 5

# Gmail search syntax (same as the Gmail search box)
$PULL "gmail datatalks" gmail list --q "from:stripe.com newer_than:30d"
$PULL "gmail datatalks" gmail list --q 'subject:(invoice OR receipt) after:2026/09/01 before:2026/10/01'
$PULL "gmail alexey.s.grigoriev" gmail list --q "has:attachment filename:pdf newer_than:60d"
$PULL "gmail datatalks" gmail list --q "in:sent to:someone@example.com" --max 10
$PULL "gmail datatalks" gmail list --label INBOX --label UNREAD

# Pagination: --max N follows nextPageToken until N messages; --max 0 = everything.
# --ids-only skips the per-message metadata request (much faster for large sweeps).
$PULL "gmail datatalks" gmail list --q "newer_than:30d" --ids-only --max 0 > ids.jsonl

# Bodies: decoded text/plain (falls back to tag-stripped HTML) + attachment list
$PULL "gmail datatalks" gmail get <message-id>
$PULL "gmail datatalks" gmail get <message-id> --out .tmp/google-pull/msg.json
$PULL "gmail datatalks" gmail thread <thread-id>   # whole conversation
$PULL "gmail datatalks" gmail raw <message-id> --out .tmp/google-pull/msg.eml
$PULL "gmail alexey.s.grigoriev" gmail attachment <message-id> <attachment-id> --out .tmp/google-pull/file.pdf
$PULL "gmail datatalks" gmail labels
```

`list` makes one request per message for metadata. For thousands of
messages, use `--ids-only` and fetch only the messages you need.

## Calendar (alexey.s.grigoriev@gmail.com)

```bash
# Next 5 events from now on the primary calendar (recurring events expanded, ordered by start)
python3 $G --as "calendar alexey.s.grigoriev" --agent admin-os calendar events --start "$(date -u +%Y-%m-%dT%H:%M:%SZ)" --max 5

# A window: dates mean 00:00 UTC; --end is exclusive; --max 0 = every page
python3 $G --as "calendar alexey.s.grigoriev" --agent admin-os calendar events --start 2026-10-01 --end 2026-10-15 --max 0
python3 $G --as "calendar alexey.s.grigoriev" --agent admin-os calendar events --start 2026-10-14T00:00:00+02:00 --end 2026-10-21T00:00:00+02:00 --max 0
python3 $G --as "calendar alexey.s.grigoriev" --agent admin-os calendar events --q "podcast" --start 2026-10-01
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
PULL="python3 $G --agent admin-os --as"
# Name search, newest first: id, name, mime, modified, size, owners, link
$PULL "drive datatalks" drive search --name newsletter --max 10
$PULL "drive alexey.s.grigoriev" drive search --name newsletter

# Content search, type filter, shared drives, raw Drive query clauses (ANDed)
$PULL "drive datatalks" drive search --fulltext newsletter --mime application/vnd.google-apps.document
$PULL "drive datatalks" drive search --name newsletter --all-drives
$PULL "drive datatalks" drive search --q "modifiedTime > '2026-09-01T00:00:00'" --max 0

# Export: Google Doc -> text/plain, Sheet -> CSV (FIRST TAB ONLY), Slides -> text;
# non-Google files (PDF, images, ...) are downloaded as-is.
$PULL "drive datatalks" drive export <doc-id> --out .tmp/google-pull/doc.txt
$PULL "drive datatalks" drive export <doc-id> --mime text/markdown --out .tmp/google-pull/doc.md
$PULL "drive alexey.s.grigoriev" drive export <sheet-id> --out .tmp/google-pull/sheet.csv
$PULL "drive alexey.s.grigoriev" drive export <pdf-id> --out .tmp/google-pull/file.pdf
$PULL "drive datatalks" drive meta <file-id>

# Every tab of a spreadsheet as CSV (Sheets API), or one A1 range
$PULL "drive alexey.s.grigoriev" drive sheet <sheet-id> --out-dir .tmp/google-pull/sheet
$PULL "drive datatalks" drive sheet <sheet-id> --range "Sheet1!A1:D50"
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
- Never print, log, or write any token. Do not pass a token on a command
  line, in the environment, or in a file. Refer to credentials only by
  location (connection reference + agent).
- Do not add scopes, create connections, or change grants to finish a pull.
  Report what the operator needs to do (see the Calendar scope gap above).
- Report counts and the minimum content needed. Mail, calendar, and Drive
  content is private.
