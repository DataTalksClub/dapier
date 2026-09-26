# AGENTS.md

Instructions for AI agents (and humans) working in this repository.

## UI/CLI parity (required)

Dapier has three surfaces, and every state-changing action must be reachable
from all of them:

- **Console (UI)** — static files in `src/web/`, talking to `/api/admin/*`
  (dispatched by `src/dapier/api/admin/`; `src/admin.py` is a compatibility shim).
- **CLI** — the `dapier` command in `dapier_cli/`, talking to `/api/agent/*`
  (handled in `src/dapier/api/agent.py`; `src/agent_api.py` is a compatibility shim).
- **HTTP API** — the Lambda handlers; the single source of truth for behavior.

**Rule: any action a user can perform in the console must also be performable
via the CLI, and both must drive it through the same HTTP API.** The console
is a convenience layer over the API — never the only way to do something.
When a feature lives on only one surface today (see "Known gaps"), the next
change that touches that area closes the gap.

Concretely:

1. **No UI-only logic.** Business logic — validation, grants, state changes —
   lives in the API handlers. `src/web/js/main.js` and the view modules under
   `src/web/js/views/` render state and call endpoints; they never implement
   an action the API does not expose.
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

On Windows, `make test` currently relies on the Unix `env` utility. Run its
equivalent from PowerShell by setting the variables and invoking pytest through
`uv`:

```powershell
$env:PYTHONPATH = "."
$env:AWS_ACCESS_KEY_ID = "testing"
$env:AWS_SECRET_ACCESS_KEY = "testing"
$env:AWS_DEFAULT_REGION = "eu-west-1"
$env:DAPIER_SKIP_CONFIG_DB = "1"
uv run --with boto3 --with pytest --with pyyaml --with 'pyjwt[crypto]' pytest -q
```

### Auditing parity

List the endpoints the console calls (`rg -n '/api/' src/web/js/`) and the
subcommands registered in `dapier_cli/main.py`. Every mutating console call
needs a CLI counterpart that reaches the same API behavior, and every CLI
command needs the console to be able to reach the same outcome — directly or
through the console's existing views. The console and CLI may use their
respective `/api/admin/*` and `/api/agent/*` routes, but handlers must share
the same domain behavior rather than duplicate business rules.

### Known gaps

None. The gaps this rule started with — console Credentials, Grants, Overview,
and token revoke — were closed with operator-gated `/api/agent/*` endpoints
and matching CLI commands. Connection setup and maintenance use
`dapier connections create|edit|scopes|import|connect|revoke`; OAuth client
values use `dapier oauth-clients set`. When a change touches one of the three
surfaces, re-run the parity audit above before merging so the list stays empty.
