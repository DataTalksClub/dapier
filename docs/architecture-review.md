# Architecture review: can Dapier scale into a real Zapier?

Reviewed 2026-09-25 against `src/dapier/` (engine, triggers, connections, api),
`designer/src/`, `workflows/`, and `template.yaml`.

## Verdict

The bones are right for a Zapier-like product: a versioned event envelope,
connector/event-triggered workflows, YAML (git-versioned) definitions with a
runtime overlay for instant publish, durable SQS delivery with DLQs, lease-based
idempotency, and run history. What exists today is a solid **"trigger → fan out
to a linear action chain"** engine.

What it is **not** yet built for is breadth: many connectors and many
user-authored workflows. The two things that don't scale are (1) connector
knowledge is smeared across four hardcoded registries in two languages, and
(2) the engine executes workflows with no data flow between steps and O(n)
workflow resolution per event. Both are fixable without rewriting the storage
or deployment model.

## What exists today

- **Event envelope** — `schema_version, id, correlation_id, connector, event,
  source, occurred_at, data`. Produced by `worker.normalize_payload`, which
  hardcodes each source (SNS/inbound-email v1, html-renderer v1) in one branch
  chain (`engine/worker.py:15`).
- **Workflow model** — YAML docs: `id`, one-or-more `triggers`
  (`connector` + `event` + simple `filters`: equals/in/prefix/suffix/contains),
  `actions` inline or a named `flow` reference resolved against the bundled
  catalog (`engine/matching.py`).
- **Four workflow sources merged per worker invocation** — bundled YAML
  (deploy-time), `PUBLISHED_WORKFLOWS_TABLE` (designer saves, overlay by id),
  and the email/hook/schedule trigger tables (`matching.all_workflows`).
- **Eight action types** — webhook, slack, telegram_send, email_send, dataops,
  dropbox_upload, dropbox_delete, render_html_to_pdf, dispatched by an if/elif
  chain in `engine/__init__.py:46`. Each is a `run_x(action, event)` module.
  Templates (`{field}` via `str.format_map`) read **only the trigger event's
  data**; action outputs are recorded on the run but never fed to later steps.
- **Trigger ingress families** — schedule (EventBridge rules, one per trigger),
  hook (`/hooks/webhook/{name}`, `/hooks/telegram/{name}`), email (Datamailer
  SNS → SQS), plus bespoke intake adapters (Dropbox resolver function +
  queue, YouTube WebSub). Each family has its own table, env var, queue, and
  special cases in `api/router.py`.
- **Connections** — OAuth providers (Google, Dropbox), token refresh,
  credentials in Secrets Manager/DynamoDB. But actions resolve auth
  inconsistently: `credential_id` (slack), `connection_id` (dropbox,
  telegram), `auth_secret_id` (dataops).
- **Designer catalog** — `designer/src/catalog.ts` mirrors the engine dispatch
  by hand ("Mirrors the run_* dispatch in src/dapier/engine/__init__.py"); validation lives in
  a third copy, `ACTION_SPECS` in `triggers/email_triggers.py:33`.

## Limitations, ranked

1. **Four parallel registries per connector, two languages.** Adding one
   action today touches: `engine/actions/<x>.py`, the `run_x` import + dispatch
   in `engine/__init__.py`, `ACTION_SPECS` validation, and
   `designer/src/catalog.ts` fields. Adding one trigger source touches:
   `worker.normalize_payload`, a new table + env + queue in `template.yaml`,
   routing in `api/router.py`, and a `load_workflows()` hook in
   `matching.all_workflows`. Nothing links these copies; they drift silently.
2. **No data flow between steps.** Actions receive the same trigger event;
   outputs go to run history only. The one async pattern that exists
   (render → completion event) only works because the renderer echoes
   `source_event` back and the completion re-matches workflows — implicit,
   per-connector magic, not an engine capability. This blocks data mapping
   (#6), logic steps (#7), and code steps (#12) from ever being clean.
3. **Workflow resolution is O(all workflows) per event.** `all_workflows()`
   scans four DynamoDB tables (published table capped at `SCAN_LIMIT = 200`
   — silently truncates) then linear-filters every workflow per event. Fine at
   ~50 workflows; wrong at hundreds.
4. **Single synchronous chain execution.** One worker invocation runs the whole
   chain under a 30 s timeout; a mid-chain failure fails the run and SQS
   redelivery replays earlier actions (idempotency leases mask it, but there
   is no per-action retry/backoff, no continue-from-step, no delays — all
   prerequisites for #7).
5. **Per-source infra surgery.** Queues, DLQs, alarms, tables, and functions
   are hand-wired per integration in `template.yaml` (Dropbox is already a
   special case with its own queue and resolver function). Every new
   integration is an infra change plus code change.
6. **Auth resolution is per-action folklore** (see connections above); no
   shared "get me a live token for this connection" service that actions can
   assume, so each new SaaS action re-implements credential lookup.
7. **Single-tenant shape.** Global workflow-id namespace, connections and
   triggers not scoped to an owner. Fine for the current shared workspace
   decision, but worth keeping out of the way of the schema now.

## Recommended reorganization: connector-centric registry

Keep: the event envelope, YAML + git as the definition format, the published
overlay, SQS/DLQ delivery, leases, runs, the designer.

1. **One connector package = one place to add an integration.**
   `src/dapier/connectors/<name>/` exposes a manifest plus implementations:
   - `manifest`: name, label, icon, emitted events, offered actions with a
     field schema (key, label, required, widget type) — the same shape the
     designer inspector needs;
   - `normalize(raw) -> event envelope` for each ingress it owns;
   - `actions`: `run(action, ctx) -> output` per action type.
   A `connectors/registry.py` collects them. The if/elif dispatch,
   `ACTION_SPECS`, and trigger-table `load_workflows` hooks all collapse into
   registry lookups.
2. **Serve the catalog to the designer.** `GET /api/catalog` emits the
   manifests as JSON; the designer fetches it and renders palettes/inspector
   fields from it (keep `catalog.ts` as a dev fallback). One registry,
   three consumers: engine dispatch, validation, UI.
3. **Introduce an `ActionContext`.** `run(action, ctx)` where ctx carries the
   event, workflow, a connection resolver (`ctx.connection("slack")` → live
   token, auto-refreshed), the run id, and a `steps` namespace. Even before
   branching exists, write outputs into `steps[<action_id>]` and let templates
   address them — that single change unblocks #6 and makes the render-completion
   pattern expressible as a normal workflow.
4. **One runtime workflows table with a trigger index.** Merge
   published/email/hook/schedule stores into one table keyed by workflow id
   with a GSI on `connector#event`. Matching becomes a query per event instead
   of four scans + a linear pass. Bundled YAML still overlays/underlays the
   same way, so instant publish semantics don't change.
5. **Generic ingress.** One table mapping ingress route/queue/source →
   connector id; one generic ingress handler; connectors declare what they
   need (a hook path, a queue consumer, a schedule family). New integrations
   stop being `template.yaml` surgery; per-connector queues remain possible
   as a declared resource, not a hand-wired special case.
6. **Plan the two-phase execution model before #7.** To support delays,
   branching, and per-step retries the worker must be able to park a run and
   resume it: persist a run/step cursor and re-enqueue "run workflow W, step k,
   with inputs" instead of replaying the whole chain on redelivery. Design the
   execution-record shape now even if you ship it later.

## Migration order (incremental, no big bang)

1. Registry wrapping the existing `run_*` functions (each action module becomes
   a connector's action; dispatch reads the registry). Tests keep passing.
2. Move `normalize_payload` branches into connectors; keep the SQS contract.
3. Emit `/api/catalog` from manifests; designer consumes it; delete the TS
   mirror and `ACTION_SPECS` duplication.
4. `ActionContext` + `steps` namespace + unified `connection_id` auth
   (keep `credential_id`/`auth_secret_id` as deprecated aliases).
5. Single workflows table + trigger GSI; migrate the four stores.
6. Then build roadmap #6/#7/#10 on top — they become small instead of
   structural.

## Status (2026-09-25)

- **Issue coverage:** the src/ package reorg already shipped (closed issues
  #1/#3, which left the back-compat shims — now deleted). The open issues
  (#6, #7, #8, #9, #10, #12, #13) are features, not this refactor; nothing in
  the issue tracker or in-flight branches covers the connector registry.
  The registry work in "Recommended reorganization" is filed as
  [issue #22](https://github.com/DataTalksClub/dapier/issues/22).
- **Shims removed:** the 23 `src/*.py` back-compat shims are deleted; the two
  test imports that still used them (`test_agent_api.py`, `test_authz.py`)
  now import canonical `src.dapier.*` paths. Lambda handlers in
  `template.yaml` already pointed at canonical modules. `make test`: 426 pass.
