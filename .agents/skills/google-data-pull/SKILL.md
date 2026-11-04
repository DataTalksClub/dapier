---
name: google-data-pull
description: Read Google data (Gmail, Calendar, Drive/Docs/Sheets) for Alexey's accounts through the dapier CLI — `dapier gmail`, `dapier gdrive`, `dapier calendar` with explicit --account selection and `dapier gdocs` for Doc text. Covers account discovery, search/pagination/bodies/attachments, bounded calendar windows, free/busy, Drive search and export. gpull.py remains the fallback for raw .eml, unbounded sweeps, and multi-tab Sheets CSV. Use when asked to read, search, export, or summarize mail, calendar events, or Drive files.
---

# Pull Google data through Dapier (read-only)

Dapier holds the Google OAuth connections; refresh tokens and client secrets
never leave Dapier. The CLI talks to the agent API server-side — you never
touch a token. Most reads are one `dapier` command now (2026-11-04,
dapier PR #36); `gpull.py` (next to this file) stays for the operations the
CLI does not cover (raw `.eml`, unbounded sweeps, multi-tab Sheets CSV).

Run everything from the dapier repo root:

```bash
uv run dapier gmail --help
uv run dapier gdrive --help
uv run dapier calendar --help
```

## Account discovery and selection (required)

List the connected Google accounts per service, then pass one explicitly on
every command:

```bash
uv run dapier gmail accounts      # email, connection id, refs per account
uv run dapier gdrive accounts
uv run dapier calendar accounts
```

- `--account` takes the verified email (`alexey@datatalks.club`) or the
  connection id (`google-gmail-datatalks`) — a unique fragment works
  (`--account datatalks`).
- The choice is required even with one connected account; an omitted
  or ambiguous selection is rejected with the candidate list. Never guess:
  the CLI will not silently pick for you, and you must not either.
- One Google sign-in serves several services; the accounts lists are
  service-filtered (a Gmail-only connection does not appear under
  `gdrive accounts`).

Current connections (verify before relying on this):

| Account | Connection id | Serves |
|---|---|---|
| alexey.s.grigoriev@gmail.com | `google-calendar` | gmail, calendar, drive (personal) |
| alexey@datatalks.club (mail) | `google-gmail-datatalks` | gmail only |
| alexey@datatalks.club (Drive) | `google-sheets` | drive, docs, sheets |
| orin@datatalks.club | `google-calendar-orin` | calendar |

```bash
uv run dapier connections list            # account, services, health
uv run dapier connections show google-calendar   # scopes, expiry (no secrets)
```

## Gmail

```bash
# newest messages / Gmail-search-syntax search (id, from, date, subject table)
uv run dapier gmail list --account datatalks --max 25
uv run dapier gmail search "from:stripe.com newer_than:30d" --account datatalks
uv run dapier gmail search "has:attachment filename:pdf newer_than:60d" --account alexey.s.grigoriev
uv run dapier gmail list --account datatalks --label INBOX --max 50

# one message: headers, decoded text body, attachment list
uv run dapier gmail read MESSAGE_ID --account datatalks
uv run dapier gmail read MESSAGE_ID --account datatalks --save-attachments .tmp/google-pull/att
uv run dapier gmail attachment MESSAGE_ID ATTACHMENT_ID --account datatalks --out .tmp/google-pull/file.pdf
uv run dapier gmail thread THREAD_ID --account datatalks      # whole conversation
uv run dapier gmail labels --account datatalks

# pagination: --max caps a page (API cap 100); continue with the printed token
uv run dapier gmail search "newer_than:30d" --account datatalks --max 100 \
  --page-token <token-from-the-previous-listing>
```

## Calendar (alexey.s.grigoriev@gmail.com)

```bash
uv run dapier calendar calendars --account alexey.s.grigoriev

# bounded window — both bounds required, span <= 366 days, occurrences expanded
uv run dapier calendar events --account alexey.s.grigoriev \
  --time-min "$(date -u +%Y-%m-%dT%H:%M:%SZ)" --time-max 2026-11-11T00:00:00Z --max 5
uv run dapier calendar events --account alexey.s.grigoriev \
  --time-min 2026-10-01T00:00:00Z --time-max 2026-10-15T00:00:00Z --query podcast

# free/busy for one or more calendars
uv run dapier calendar freebusy --account alexey.s.grigoriev \
  --calendar primary --time-min 2026-11-04T00:00:00Z --time-max 2026-11-05T00:00:00Z

# one event
uv run dapier calendar event EVENT_ID --account alexey.s.grigoriev
```

Known scope gap (unchanged): the `google-calendar` connection carries
`calendar.events.owned` + `calendar.freebusy` but **not**
`calendar.readonly`, so `calendar calendars` may fail with the provider's
403 (the CLI relays it with a `connections edit --scopes` hint) while
`events` works on owned calendars including `primary`. An operator adds the
scope and re-consents; do not work around it.

## Drive, Docs, Sheets

```bash
# name/Drive-query search, newest first
uv run dapier gdrive search "name contains 'newsletter'" --account datatalks --max 10
uv run dapier gdrive search "fullText contains 'newsletter'" --account datatalks
uv run dapier gdrive list --account datatalks --max 25

# metadata, download/export (Google-native: doc/slide -> PDF, sheet -> CSV, drawing -> PNG)
uv run dapier gdrive meta FILE_ID --account datatalks
uv run dapier gdrive download DOC_ID --account datatalks --out .tmp/google-pull/doc.pdf
uv run dapier gdrive download DOC_ID --account datatalks --export-mime text/plain --out .tmp/google-pull/doc.txt
```

Docs text read/write stays on `dapier gdocs` (token minted in-process,
never printed):

```
dapier gdocs get <doc-url-or-id>                       # title, revision, plain text
dapier gdocs append <doc> --file text.txt              # or --text "..." or stdin
dapier gdocs replace <doc> --find "PLACEHOLDER" --text "new"
```

Default `--as "drive datatalks"`; personal docs: `--as "drive
alexey.s.grigoriev"`. The account must have edit access. Write only when
the user asked for that write.

## gpull.py fallback: what the CLI does not cover

`gpull.py` (stdlib-only, GET-only, token in a local variable) remains the
tool for:

1. **Raw messages** as `.eml`: `gmail raw <message-id> --out msg.eml`.
2. **Unbounded sweeps**: `gmail list --ids-only --max 0` walks every page
   without per-message metadata requests (the CLI caps a page at 100).
3. **Every spreadsheet tab as CSV** via the Sheets API
   (`drive sheet <sheet-id> --out-dir …`), preserving numeric amounts and spreadsheet-formatted
   dates — DataOps bookkeeping keys on those filenames. `gdrive download`
   on a sheet exports only the first tab.

```bash
G=.agents/skills/google-data-pull/gpull.py
python3 $G --as "gmail datatalks" --agent admin-os gmail list --q "newer_than:30d" --ids-only --max 0 > ids.jsonl
python3 $G --as "gmail datatalks" --agent admin-os gmail raw <message-id> --out .tmp/google-pull/msg.eml
python3 $G --as "drive alexey.s.grigoriev" --agent admin-os drive sheet <sheet-id> --out-dir .tmp/google-pull/sheet
```

Custom scripts may still mint one token in-process via `dtoken.py` — for
the covered operations, prefer the CLI (no token handling at all):

```python
import sys
sys.path.insert(0, ".agents/skills/google-data-pull")
import dtoken
access_token = dtoken.dapier_token("gmail datatalks", "admin-os")
# ... Authorization: Bearer {access_token} ...
del access_token
```

The DataOps regression harness is unchanged:
`python3 .agents/skills/google-data-pull/run_dataops_contract.py` (mocks
the Sheets API, dry-runs the pinned DataOps PR; see its header).

## Output conventions

- The CLI prints tables by default and `--json` for raw output; `gpull.py`
  prints JSONL to stdout. Pipe to `jq`/`python3` or redirect.
- Files go to `--out` paths you name; keep pulled data under
  `.tmp/google-pull/` (gitignored) or a scratch directory outside the repo.
  Never commit pulled mail, events, or documents.
- Provider errors exit non-zero with the API's message (plus a
  `connections edit --scopes` hint on 403s). Tokens never appear anywhere.

## Read-only safety rules

- Only read. Do not send, draft, label, archive, trash, or delete mail. Do
  not create, edit, or delete events. Do not create, edit, share, move, or
  trash Drive files. **The new CLI nouns can write** (`gmail send/reply`,
  `gdrive upload/share/trash`, `calendar create/update/delete`; composition
  and selected Drive/Calendar writes have `--preview`, Calendar uses
  `--send-updates`), but that does not change this
  rule: use those verbs only when the task explicitly asks for exactly that
  write.
- Exceptions stay as before: `dapier gdocs append|replace` when the user
  asked for that Doc edit.
- Never print, log, or write any token; `dtoken` tokens live in a local
  variable only. Do not pass tokens on a command line, in the environment,
  or in files.
- Do not add scopes, create connections, or change grants to finish a pull.
  Report what the operator needs to do (see the Calendar scope gap above).
- Report counts and the minimum content needed. Mail, calendar, and Drive
  content is private.
