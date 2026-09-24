# AGENTS.md

Instructions for AI agents (and humans) working in this repository.

## UI/CLI parity (required)

Dapier has three surfaces, and every state-changing action must be reachable
from all of them:

- **Console (UI)** — static files in `src/web/`, talking to `/api/admin/*`
  (handled in `src/admin.py`).
- **CLI** — the `dapier` command in `dapier_cli/`, talking to `/api/agent/*`
  (handled in `src/agent_api.py`).
- **HTTP API** — the Lambda handlers; the single source of truth for behavior.

**Rule: any action a user can perform in the console must also be performable
via the CLI, and both must drive it through the same HTTP API.** The console
is a convenience layer over the API — never the only way to do something.
When a feature lives on only one surface today (see "Known gaps"), the next
change that touches that area closes the gap.

Concretely:

1. **No UI-only logic.** Business logic — validation, grants, state changes —
   lives in the API handlers. `src/web/app.js` renders state and calls
   endpoints; it never implements an action the API does not expose.
2. **Ship parity in the same change.** Adding or changing a console action
   means adding or changing the API endpoint and the CLI command in the same
   PR. Do not merge a console-only feature with "CLI support to follow".
3. **The CLI is a thin client.** CLI commands call the API the same way the
   console does (via `dapier_cli/api.py`). The CLI must not reimplement or
   bypass API behavior, e.g. by writing to DynamoDB directly.
4. **Test both paths.** An endpoint backing a console action gets tests in
   `tests/` for the API behavior and for the CLI command that wraps it
   (`make test`).
5. **Exceptions are explicit.** Purely presentational console behavior
   (client-side sorting/filtering, layout, charts) needs no CLI counterpart.
   Anything that mutates state must follow the rule. If parity genuinely
   cannot hold for an action, say why in the PR and get explicit agreement
   before merging.

### Auditing parity

List the endpoints the console calls (grep `src/web/app.js` for `/api/`) and
the subcommands registered in `dapier_cli/main.py`. Every mutating console
call needs a CLI counterpart that hits the same endpoint family, and every
CLI command needs the console to be able to reach the same outcome — directly
or through the console's existing views.

### Known gaps

None. The gaps this rule started with — console Credentials, Grants, Overview,
and token revoke — were closed with operator-gated `/api/agent/*` endpoints
and the `dapier credentials set`, `dapier grants list|save|delete`,
`dapier overview`, and `dapier connections revoke` commands. When a change
touches one of the three surfaces, re-run the parity audit above before
merging so the list stays empty.
