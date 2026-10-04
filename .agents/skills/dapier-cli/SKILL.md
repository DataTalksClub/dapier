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

- API base: `$DAPIER_API_URL` → `api_url` in `~/.config/dapier/config.json`
  → default `https://dapier.dtcdev.click`.
- Session tokens: `~/.config/dapier/session.json` (DTC-issued; the file is
  owner-only). On 401s, re-authenticate with `uv run dapier auth --help`.
- Headless consumers use operator-issued API tokens instead
  (`uv run dapier tokens --help`).

## Command groups

`auth`, `connections`, `token`, `credentials`, `grants`, `tokens`,
`oauth-clients`, `overview`, `usage`, `quota`, `errors`, `agent-tasks`,
`workers`, `worker`, `catalog`, `runs`, `inbox`, `audit`, `storage`,
`workflows`, `webhooks`, `schedules`, `polls`, `emails`.

(`triggers` and `hooks` are aliases that point at `emails`/`webhooks`.)

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
uv run dapier inbox list --limit 10
```

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
