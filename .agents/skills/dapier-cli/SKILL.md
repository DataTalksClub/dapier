---
name: dapier-cli
description: How to locate, authenticate, and drive the dapier CLI against the live API — config paths, command groups, and recipes for inspecting runs, workflows, email addresses, and connections. Use when an agent shell cannot find the `dapier` command, or when asked to check live state (runs, failures, workflows, emails, usage) or change it from the terminal.
---

# The dapier CLI

The CLI is the terminal surface over the same HTTP API the console uses.
It is a thin client: every command maps to a `/api/agent/*` endpoint
(operator-gated); the console drives `/api/admin/*`. Neither surface
reimplements behavior — parity rules live in AGENTS.md.

## Running it

The `dapier` executable is NOT on an agent shell's PATH. Run it from the
repo root through uv (entry point `dapier_cli.main:main`):

```bash
uv run dapier --help            # global help + command groups
uv run dapier <group> --help    # per-group help
uv run dapier --api-url URL ... # override the API base for one call
uv run dapier --debug ...       # print requests (never credentials)
```

No local AWS credentials are needed — the CLI talks to the live API
directly.

## Config and auth

- API base: `--api-url` → `$DAPIER_API_URL` → default
  `https://dapier.dtcdev.click`.
- Session tokens: `~/.config/dapier/session.json` (DTC-issued; the file is
  owner-only). On 401s, re-authenticate with `uv run dapier auth --help`.
- Headless consumers use operator-issued API tokens instead
  (`uv run dapier tokens --help`).

## Command groups

`auth`, `connections`, `credentials`, `grants`, `tokens`,
`oauth-clients`, `overview`, `usage`, `quota`, `errors`, `agent-tasks`,
`workers`, `worker`, `catalog`, `runs`, `audit`, `storage`,
`workflows`, `webhooks`, `schedules`, `polls`, `emails`, `slack`,
`gdocs`, `gmail`, `gdrive`, `calendar`.

Scripts needing a provider token fetch it in-script via the `dtoken.py`
helper in the `google-data-pull` skill — there is no `token exec`
anymore. For most Google reads and writes the `gmail`/`gdrive` nouns are
the shorter path (no token handling at all; see below).

## Recipes

```bash
# What ran, and why something failed
uv run dapier runs list --workflow invoice-intake --limit 5
uv run dapier runs show invoice-intake:<run-id>     # per-step input/error
uv run dapier errors                                 # failures by workflow
uv run dapier runs replay-failed <workflow-file>     # re-inject triggers

# Workflow lifecycle: save makes a draft, publish goes live
uv run dapier workflows list                         # On/Off + draft state
uv run dapier workflows show <file>                  # live definition (JSON)
uv run dapier workflows export <file>                # canonical YAML
uv run dapier workflows save <path.yaml>             # draft (nothing live)
uv run dapier workflows publish <file>               # draft -> live
uv run dapier workflows on|off|delete <file>         # state / removal
uv run dapier workflows test|test-step <file>        # dry-run against sample
uv run dapier workflows test-code <file> --action <id>  # code step tests
uv run dapier workflows versions|diff|rollback <file>

# Live email addresses (one per flow trigger route — derived, not hand-managed)
uv run dapier emails list
uv run dapier emails show invoice

# Incoming events, matched or not
uv run dapier runs events list --limit 10

# Gmail over a stored Google connection (token stays server-side)
uv run dapier gmail accounts                       # Google accounts: email + connection id
uv run dapier gmail search "from:boss is:unread" --account me@gmail.com
uv run dapier gmail read MSG_ID --save-attachments ./att
uv run dapier gmail send --to a@b.c --subject Hi --file body.txt --attach r.pdf --account me@gmail.com
uv run dapier gmail reply MSG_ID --draft --text "On it"     # reply draft, thread preserved
uv run dapier gmail reply MSG_ID --all --text "Thanks all"  # direct reply-all send
uv run dapier gmail draft create --to a@b.c --subject S --text ... --attach inv.pdf
uv run dapier gmail draft send DRAFT_ID
uv run dapier gmail mark MSG_ID --read --archive

# Google Drive over a stored Google connection
uv run dapier gdrive accounts
uv run dapier gdrive list --account me@gmail.com --max 20
uv run dapier gdrive download FILE_ID --out ./out.pdf      # native docs export (PDF/CSV/PNG)
uv run dapier gdrive upload ./f.txt --name f.txt --folder FOL_ID
uv run dapier gdrive share FILE_ID --role writer --with user --email p@x.y
uv run dapier gdrive trash FILE_ID                          # --permanent destroys
uv run dapier gdrive restore FILE_ID

# Google Calendar (guest email only ever via --send-updates; default none)
uv run dapier calendar calendars --account me@gmail.com
uv run dapier calendar events --time-min 2026-11-04T00:00:00Z --time-max 2026-11-11T00:00:00Z
uv run dapier calendar freebusy --calendar primary --time-min ... --time-max ...
uv run dapier calendar create --summary Sync --start 2026-11-05T09:00:00Z \
    --end 2026-11-05T10:00:00Z --attendee peer@example.com --preview   # review first
uv run dapier calendar create ... --send-updates all        # actually invite
uv run dapier calendar update EVENT_ID --summary Renamed --calendar primary
```

With several Google accounts connected, `--account` (verified email or
connection id) is required on `gmail`/`gdrive`/`calendar` operations;
ambiguous or missing selections are rejected with the candidate list.
Insufficient-scope operations fail with the provider's 403 plus a
`dapier connections edit --scopes` hint. Write verbs accept `--preview`
to print the exact request body without sending; calendar event windows
are bounded (≤ 366 days) and guests are only emailed via `--send-updates`.

## Gotchas

- Email addresses follow flow triggers: registering/removing a route is
  done by the workflow's trigger filter (`route: equals: <name>`), so
  `emails` has no create/delete — save/publish/delete the workflow instead.
- The inbox has no delete; events can only be listed, shown, or replayed.
- Workflow saves create drafts; publishing promotes them. The live store's
  GitHub sync is not configured, so store-side deletes do not commit —
  mirror repo `workflows/*.yaml` changes locally and commit them yourself.
- Deleting a live workflow removes its derived email address immediately.
- Tests for this repo: `make test` (see AGENTS.md for the Windows/uv
  variant).
