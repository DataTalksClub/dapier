# Plugin system for connections, triggers, and actions — migration plan

Status: plan only (no code changes yet). Written 2026-10-03.

## Goal

Each integration (its connection provider, trigger chips/ingress, actions,
discovery, health check, docs, and tests) lives in its own folder with its
own version and test lane, inside this one mono-repo. Core (engine, API,
CLI, console, auth, connections storage) stays in `src/dapier` and changes
independently of any integration. One deploy pipeline remains; "own release
cycle" means independent versioning, changelogs, and CI per plugin — not
separate Lambda deployments (that is explicitly out of scope for now).

## What exists today (the seams a plugin system formalizes)

Dapier already has a single registry contract (`src/dapier/connectors/registry.py`)
and the docstring there says "one place to add an integration". Adding or
moving an integration touches these registration points:

| Concern | Where it lives today | Registration |
|---|---|---|
| Action catalog + runners wiring | `src/dapier/connectors/<name>.py` | `registry.register(Action(...))` |
| Runner implementations | `src/dapier/engine/actions/<name>.py` | imported by the connector module |
| Trigger chips (palette) | `src/dapier/connectors/triggers.py` + per-module | `registry.connector(Connector(...))` |
| Ingress normalizers | `src/dapier/connectors/ingress.py` | `@ingress`, order-sensitive |
| Poll sources | `src/dapier/triggers/poll_sources.py` | `register_source(PollSource(...))` + lazy `_BUILTIN_MODULES` tuple |
| Trigger sample/option discovery | `src/dapier/connectors/trigger_discovery/` | per-connector registration (`TRIGGER_DISCOVERIES`) |
| Connection-scoped discovery + health checks | connector modules | `register_discovery` / `register_connection_test` |
| OAuth provider adapters | `src/dapier/connections/providers/oauth_providers.py` (+ `oauth_clients.py`, `slack_tokens.py`, `telegram_api.py`), `connections/zoom.py` | module-level adapter dicts |
| Provider→discovery mapping, test aliases | `registry.PROVIDER_DISCOVERY_SOURCES`, `CONNECTION_TEST_ALIASES` | hard-coded dicts in the registry |
| Load point | `src/dapier/connectors/__init__.py` | hand-maintained import list |
| Designer mirror | `designer/src/catalog.ts` | hand-synced, enforced by `tests/test_designer_catalog.py`; live surfaces read `GET /api/catalog` |

Scale: `connectors/` + `engine/actions/` + `connections/` + `triggers/` is
~26k LOC across ~70 modules; `zoom/` and `trigger_discovery/` are already
sub-packages, so the "folder per integration" shape exists in embryo.

Consumers that must not change: the engine dispatches through
`registry.run_action`; save-time validation through `registry.validate_action_chain`;
`GET /api/catalog` serves `registry.catalog()`; the CLI discovery nouns and the
console render from the same catalog. All of these are registry-driven and
plugin-agnostic by construction.

Packaging reality: one SAM app (`template.yaml`, `CodeUri: .`), one API
Lambda + worker + a few intake Lambdas (`email_ingress`, `youtube_subscriptions`,
`dropbox_resolver`, `alarm_notify`, `backup`, `error_digest`), one `make test`
suite (~214 files, 3700+ tests), one deploy workflow (`.github/workflows/deploy.yml`
→ `make test` + `scripts/build-sam.sh` + deploy). Any plugin layout must keep
this true: `sam build` copies the repo, so `plugins/` at the repo root is
bundle-included for free; the js-runtime layer and `WORKFLOWS_DIR` default are
unaffected.

## Proposed layout

```
plugins/
  google/            # one OAuth connection backs sheets, drive, calendar,
    plugin.yaml      #   gmail, youtube — connection granularity = plugin granularity
    version          # semver
    CHANGELOG.md
    plugin.py        # registration module (today's connectors/sheets.py et al. merged)
    runners/         # today's engine/actions/{sheets,drive,calendar,gmail,youtube}.py
    providers/       # OAuth adapter glue if any (shared Google client lives in core)
    intake/          # today's triggers/intake/youtube_subscriptions handler
    tests/           # the plugin's tests, moved from tests/
    README.md        # from docs/connectors/google.md + youtube.md
  dropbox/
  slack/
  telegram/
  zoom/              # includes today's connectors/zoom/ sub-package + connections/zoom.py
  mailchimp/         # pseudo-connection (stored API key)
  aws/               # s3/aws pseudo-connections (bucket actions + poll source)
  rss/
```

Core keeps everything that is not a provider integration, in `src/dapier`:
engine (incl. `engine/logic.py` logic steps and the in-flight `logic_pkg`
split), `connectors/registry.py` (the plugin API), generic connectors
(`code`, `csv`, `date_time`, `render`, `digest`, `subworkflow`, `schedule`,
`poll`, `webhook`, `email` — SES contract, DataOps forwarding, bounce/
complaint lifecycle are engine-adjacent), `ai` (env-config, no connection),
`connections/` storage + credentials + OAuth flow machinery, `triggers/`
storage and dispatch, API, CLI, console.

### plugin.yaml (manifest)

```yaml
name: slack
version: 1.4.2
core_compat: ">=1.0"        # range check against a core version constant
provides:
  providers: [slack]        # connection provider keys (uniqueness enforced)
  provider_aliases: {}      # e.g. aws plugin: {s3: aws}
  discovery_sources:        # replaces registry.PROVIDER_DISCOVERY_SOURCES entries
    slack: [slack]
  test_aliases: {}          # replaces CONNECTION_TEST_ALIASES entries
  trigger_chips: [slack]
  ingress:                  # module + priority (lower runs first; email contract = 0)
    - {module: plugin, priority: 100}
  poll_sources: [slack]
  intake_handlers: []       # SAM Handler paths this plugin owns, if any
```

The manifest is data the core loader validates (unique names, unique
provider keys, resolvable modules); the registry stays the runtime API.

### Loader

New small package `src/dapier/plugins/` (loader + manifest schema + core
version constant). It scans `plugins/*/plugin.yaml`, sorts by name,
imports each `plugin.py` (import = registration, same idiom as today),
checks manifest contracts, and fails loudly on collisions. The two
hand-maintained lists die:

- `connectors/__init__.py` keeps importing `registry` + generic core
  connectors only; provider modules no longer appear there.
- `poll_sources._BUILTIN_MODULES` is replaced by loader-fed registration
  (poll sources register at plugin import; `resolve` no longer lazy-imports
  provider modules — the loader already imported every plugin).

Ingress keeps its order guarantee via the manifest `priority` (email's
normalizer is declared priority 0 in core; plugin normalizers default 100).
Everything else is order-free (registry dicts keyed by type/name).

## Release cycle (mono-repo, one pipeline)

- `plugins/<name>/version` + `CHANGELOG.md` per plugin; a plugin release =
  bump its version in a PR; `main` push deploys it like today.
- Loader logs a warning (and `dapier doctor`-style check reports) when
  `core_compat` is unsatisfied — catches "plugin moved faster than core".
- Optional later: git tags `plugin/<name>/vX.Y.Z` if we ever need to point
  at a plugin's exact released state; per-plugin Lambda layers / separate
  SAM stacks stay out of scope until a concrete need appears.
- CI: `deploy.yml` gains a path-filtered test matrix — one job per plugin
  (`pytest plugins/<name>/tests`, triggered when `plugins/<name>/**` or
  shared fixtures change) plus an always-run core job (`tests/` + loader
  smoke: import every plugin, catalog builds). The deploy gate stays the
  full `make test`, so the live contract is unchanged.

### Testing

- Per-plugin tests move from the flat `tests/` into `plugins/<name>/tests/`
  (clearly per-plugin files: `test_mailchimp_*`, `test_calendar_actions`,
  `test_dropbox_slack_actions`-style splits reviewed per plugin).
- Shared fakes (moto/boto3 setup, OAuth transport fakes, registry reset
  helpers) move to a common helper (e.g. `tests/plugins_common/` or
  `src/dapier/plugins/testing.py`) that plugin tests import; registry
  isolation follows the existing poll-sources pattern (fresh dicts per test).
- `tests/test_designer_catalog.py` (catalog.ts mirror) and the parity audit
  keep working unchanged — they read the registry, wherever plugins loaded
  from.
- Test runner: `make test` (all) unchanged; `pytest plugins/slack` for one
  lane; loader smoke test asserts catalog parity before/after the move so
  no connector silently disappears mid-migration.

## Migration phases (strangler, each phase shippable)

Phase 0 — loader + manifests, zero moves. Add `src/dapier/plugins/`,
manifest schema, core-version constant, and loader smoke tests. Keep every
built-in where it is; `connectors/__init__.py` and `_BUILTIN_MODULES` still
work. Ship the doghouse before moving any dog.

Phase 1 — pilot: slack. Move `connectors/slack.py`,
`engine/actions/slack.py`, its trigger samples, poll source,
`connections/providers/slack_tokens.py`, `docs/connectors/slack.md`, and its
tests into `plugins/slack/`. Delete its entries from the old lists; wire the
first CI matrix lane. Slack is the pilot because it is self-contained
(pasted token, no OAuth client, no intake Lambda), well-tested, and
exercises actions + chips + samples + poll source + health check + provider
glue — every contract except ingress and intake handlers.

Phase 2 — same shape, small plugins: dropbox (adds an intake handler +
OAuth), telegram, mailchimp (pseudo-connection + intake), aws (pseudo-
connection + alias), rss, zoom (sub-package move + `connections/zoom.py`).

Phase 3 — the google family last (biggest blast radius: one connection
provider serving sheets/drive/calendar/gmail/youtube, the
`youtube_subscriptions` intake Lambda, and the shared OAuth client docs).

Phase 4 — cleanup: remove the deprecated lists, re-run the UI/CLI parity
audit (`rg -n '/api/' src/web/js/` vs `dapier_cli/main.py` — no behavior
change is expected, but the rule says audit when a change touches the
surfaces), update `docs/connectors/README.md` and AGENTS.md's product-scope
notes with the plugin layout.

Each phase lands as its own focused commit(s) with `make test` green; the
catalog-smoke test guarantees the visible catalog is byte-identical before
and after every move.

## Risks and open questions

- Import cycles: connector modules import `engine.actions` runners; the
  loader must import plugins only after core is importable, and engine
  dispatch keeps its lazy registry import. Keep the existing rule: registry
  never imports the engine.
- Ingress order is the one order-sensitive registry — solved by manifest
  priority, but the smoke test should pin it.
- `engine/actions/` runners move into plugins while `engine/logic.py` (and
  the in-flight `logic_pkg/` refactor by another session) stays core: hold
  Phase 1 until that lands to avoid churn on shared files.
- Shared code between plugins (e.g. google-sheets and google-drive both use
  the Google API client helpers): put shared helpers in the family plugin
  (one `google` plugin), not in core — core gains nothing provider-shaped.
- `storage.py`/`digest.py` connectors touch shared tables (digests,
  storage) — they stay core; plugins must not create their own DynamoDB
  tables (product-scope rule: no plugin-owned business data stores).
- Designer `catalog.ts` mirror stays hand-synced (test-enforced). Optional
  follow-up, out of scope here: generate it from `GET /api/catalog`.
- OAuth client storage (`oauth-clients set`) and the credentials table are
  core; plugins only declare provider adapters. No schema changes required.

## Explicitly out of scope

Separate Lambda deployments per plugin; runtime-installed third-party
plugins; plugin marketplace/enable-disable UI; moving logic steps or the
engine into plugins; any change to the connections data model.
