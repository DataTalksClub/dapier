# Zapier gap analysis: what dapier still lacks

## Landed since this analysis (2026-09-27)

- **Discover/test/replay for connections** — `connections discover`/`connections test`
  CLI + `/api/{admin,agent}/connections/{id}/discover|test`, provider catalog in
  `connections/discovery.py` (google, youtube, dropbox, zoom, slack, telegram,
  credential-backed s3/mailchimp).
- **G1** — `sheets_find_row` landed, plus `sheets_lookup_row` and
  `sheets_update_row` beyond the sketch (`engine/actions/sheets.py`).
- **G4** — task usage metering: worker rollup into TaskUsageTable (best-effort,
  completed steps only), `GET /api/{admin,agent}/usage?months=`, overview
  3-month block, `dapier usage`.
- **G5** — run filters `workflow_id|status|since` on `GET /api/{admin,agent}/runs`
  and `dapier runs list --workflow|--status|--since`.
- **Dry-run** — `engine/dryrun.py` now derives supported types from the
  connector registry instead of the hardcoded pre-registry list.
- **Per-step live testing** — Zapier's "Test step": `engine/dryrun.py`
  `test_step` runs one action (addressed by its run-history id, so branch
  and error-branch steps work too) against a sample event — for real with
  `execute: true`, render/evaluate only without — while logic steps report
  what a run would decide (branch taken, filter pass, delay wait, loop
  size) without executing. Prior steps' outputs ride along as the
  run-history-shaped `steps` context, so `{steps.<id>.output.*}` templates
  preview and run against real data. Exposed on
  `/api/{admin,agent}/designer/workflows/test-step` (+ per-file),
  `dapier workflows test-step`, and the designer inspector's "Test step"
  (its output feeds the next step's test). Tests in `tests/test_test_step.py`.
- **G7** — default-on error visibility: a workflow with no `notify:` now
  notifies the operator address (DAPIER_EMAIL_SENDER; `notify: []` opts
  out), poll-trigger fires that fail notify the operator instead of
  passing `event=None`, and `GET /api/{admin,agent}/errors/summary` +
  `dapier errors [--days N]` report failed-run counts by workflow.
- **G3** — per-step error handling: generic `on_error: halt|continue|run`
  plus `error_actions: [...]` on any step (logic or connector), the error
  message templatable as `{steps.<id>.error}` and the recovery chain
  recorded under `<step>.error` in run history; a lighter
  `on_fail: continue|halt` (Zapier's error setting) absorbs a failure as a
  `skipped` step. Shape-checked at save time on both surfaces
  (`registry.validate_error_keys` / `validate_on_fail_key`,
  `_validate_steps` recursion); tests in `tests/test_on_error_validation.py`,
  `tests/test_on_fail.py`, `tests/test_logic.py`.
- **G10** — save-time input validation with types: `fields` entries declare
  `type: number|integer|boolean|email|url` or `enum`/`select` choices;
  `validate_action_chain` rejects clearly-wrong literals (templated values
  exempt), the designer save applies the same rules to every step
  (`registry.validate_field_types`), and the dry-run re-checks *rendered*
  inputs — required fields the sample renders empty or typed values gone
  implausible come back as step `warnings`, fatal under `strict: true`
  (API test-run body flag, `dapier workflows test --strict`). Tests in
  `tests/test_registry_types.py`, `tests/test_dryrun_strict.py`.
- **G2** — real delays (requeue-until): a `delay` step past the 60 s inline
  cap raises `RunSuspended` (the step closes out `delayed` in run history)
  and the worker parks a `{"dapier_resume": ...}` continuation on the event
  queue with `DelaySeconds = min(remaining, 900)`; longer waits chain
  re-enqueues against the absolute `resume_at`, and no total may exceed the
  executions table's 90-day retention. The resumed record is detected before
  trigger matching and replays the captured remainder through the same step
  leases and run grouping. `delay` also accepts `minutes`/`hours`/`days`
  (unit-converted) and a template-renderable `until` ISO datetime; save-time
  validation mirrors the engine (`_validate_delay`). Tests in
  `tests/test_logic.py`, `tests/test_worker.py`, `tests/test_designer.py`.
- **G8** — n-way paths: a `paths` logic step runs the first matching branch
  (`{label, when, actions}` in order, flat filter shorthand allowed, optional
  `default` steps), with sub-chain telemetry under `<step>.<label>`.
  `tests/test_paths_logic.py`.
- **G9** — trigger sample autofill: `GET /api/{admin,agent}/triggers/sample`
  returns the workflow's most recent recorded input (history-first,
  discovery-fallback) and the designer inspector fetches it as the test
  event instead of a `{"dry_run": true}` placeholder. Tests in
  `tests/test_trigger_sample.py`, `tests/test_cli_trigger_samples.py`.
- **G12** — data stores: `storage_get|set|delete|find` actions
  (`engine/actions/storage.py`, per-workflow `scope`/`key` items with
  optional TTL in StorageTable) registered in the catalog and the designer
  mirror; `GET|POST|DELETE /api/{admin,agent}/storage/{workflow}` and
  `dapier storage get|set|find|delete` drive the same `kv_*` domain layer
  the runs use, and designer test-runs exercise storage through the
  router's own table grants. Tests in `tests/test_storage_actions.py`,
  `tests/test_storage_api.py`, `tests/test_storage_cli.py`. The console side
  is the Data store view (`src/web/js/views/storage.js`): browse a
  workflow's keys by prefix, store with optional TTL, delete per key —
  verified end-to-end in a headless browser against `/api` fixtures
  (Playwright route interception; evidence in `.tmp/storage-verify/`).
- **G13** — sub-workflows: a `run_workflow` connector action runs another
  published workflow in-process (trigger bypassed, optional rendered
  `payload`, step outputs under `output_field`), nesting capped at depth 2
  so cycles fail loudly. Console inbox replay (coverage-matrix gap 1) also
  landed: a Trigger inbox view with per-event replay and envelope inspector.
- **G11** — digests: `digest_add` / `digest_flush` actions
  (`engine/actions/digest.py`) collect template-rendered items (JSON keeps
  its shape, templates render at any depth; `dedupe: true` skips repeats,
  optional TTL) under a digest key in the same per-workflow StorageTable
  the storage actions use — arrival-ordered keys, 50 pending items max —
  and `digest_flush` releases them as one `{items, count, empty}` list and
  clears, so a scheduled workflow pairs `digest_add` on events with
  `digest_flush` on the schedule and filters on `empty`. A full digest is
  a clear error telling the author to flush. Tests in `tests/test_digest.py`.
  Alongside the actions, a `digest` logic step (`engine/logic.py`) covers
  the same loop in one step type: `mode: accumulate` (the default) appends
  a rendered `item` and/or an `items` list under a required literal `key`
  (output `{"key", "digested"}`), and `mode: flush` claims-and-clears the
  batch in one atomic `delete_item(ALL_OLD)` (concurrent flushes never
  double-send; an appender racing a flush lands in the next digest),
  exposing the items to following steps as `{digest.items}` /
  `{digest.count}` (`list: digest.items` for a for_each); an empty flush
  records `skipped` with `{empty: true, count: 0}` and the chain runs on.
  State lives in DigestsTable (`engine/actions/digests.py`,
  `DIGESTS_TABLE`, scoped per workflow like StorageTable) with grants for
  the worker and the router; registered in `connectors/logic.py` and the
  designer mirror, with save-time validation in `api/designer_store.py`
  (`_validate_digest`). Tests in `tests/test_digest_logic.py`. The logic
  step also takes `shared: true`: the digest moves to a `*shared*`
  partition every workflow can reach, because dapier's one-trigger-per-
  workflow model makes the canonical pattern cross-workflow — accumulate
  in the event's workflow, flush from the schedule-triggered one. (Both
  surfaces coexist deliberately: actions for action-shaped flows on
  StorageTable, the logic step for in-chain batching on DigestsTable;
  `shared` exists on the logic step only.)
- **Suspended run lifecycle** — a parked run no longer outlives its
  workflow: `_resume_run` resolves the envelope's workflow through
  `all_workflows()` (the same lookup fresh events match against) and, if
  the workflow was unpublished/disabled/deleted, closes the paused
  `delayed` steps out `cancelled` and consumes the envelope instead of
  replaying — the remaining actions never fire. Cancelling is an operator
  verb too: `runs.api_cancel` flips the still-`delayed` steps to
  `cancelled` under a conditional write (a concurrent resume can't
  double-write; the resume drops an envelope whose pause reads
  `cancelled`), exposed as `POST /api/{admin,agent}/runs/{id}/cancel`
  (operator-gated, audited `runs.cancel`, 409 when the run isn't
  suspended), `dapier runs cancel`, and a Cancel button in the console run
  dialog with `delayed_until` surfaced on delayed list rows; the run
  rollup gained a `cancelled` status. Tests in `tests/test_delay_resume.py`,
  `tests/test_worker.py`, `tests/test_admin.py`, `tests/test_agent_api.py`,
  `tests/test_cli.py`.
- **Per-step autoretry + richer filter operators** — Zapier's autoretry:
  `autoretry: {attempts, initial_seconds, max_seconds}` on connector actions
  retries a transient failure in place with exponential backoff (the initial
  delay doubling up to max_seconds, plus jitter; attempts 1–3 and seconds
  1–60 keep the inline sleeps small), records `attempts` (tries made) in the
  step's output so run history shows a step that succeeded on try 3, and
  only then falls through to `on_fail`/`on_error` — and past both, to the
  workflow-level `retry` redrive. Opt-in per step, never auto-enabled, and
  rejected on logic steps (`registry.validate_autoretry_key`, shared by the
  engine's run-time plan parser and the designer save). The one predicate
  evaluator behind trigger filters and logic rules
  (`matching._matches_filter`) gained `not_equals`, `does_not_contain`,
  `gt`/`gte`/`lt`/`lte` (numeric when both sides parse as floats, else
  lexicographic — so ISO dates compare correctly), `exists` and `empty`
  (present = non-empty after stringifying; the expected boolean flips the
  sense), published via the catalog's `filter_operators`/`logic_operators`
  and the designer mirror; unknown operators still fail loudly. Tests in
  `tests/test_autoretry.py`, `tests/test_workflow_logic.py`.
- **Rules see earlier steps, everywhere they are written** — chain predicates
  (filter, condition, paths) and the `for_each` `list` resolve against the
  accumulated `steps` outputs next to the trigger data (the delay/digest
  contexts already did), so mid-chain routing can gate on what a search step
  found: `when: {steps.find.output.count: {gt: 0}}` or
  `list: steps.find.output.rows`. The full operator set is accepted at every
  surface that writes rules: designer saves (`designer_store.FILTER_OPERATORS`
  now mirrors `matching._matches_filter` — flat `operator:` saves with `gt`
  etc. no longer bounce), the designer mirror, and trigger filters. The
  designer save path also validates `autoretry` (`validate_autoretry_key`
  wired into `_validate_steps`, matching stored-trigger saves). Tests in
  `tests/test_filter_scopes.py`.

Still open: none — G1–G13 are all landed.

### Landed 2026-09-28 (round 4: the remaining analyst backlog)

- **HTTP auth variety** — `http_request` `auth_type: none|basic|bearer|api_key`
  (basic user/pass templated, direct bearer token or the connection fallback,
  API key in header or query), fields mirrored in the designer catalog.
  Tests: `tests/test_http_request_auth.py`.
- **Webhook payload shaping** — the `webhook` action accepts an optional
  templated `payload` (rendered, JSON-validated, HMAC-signed like the default
  whole-event body); runner takes the injected transport like `http_request`.
  Tests: `tests/test_webhook_payload.py`.
- **Trigger management parity** — hooks/schedules/polls had API routes on both
  dispatchers but no surfaces: `dapier hooks|schedules|polls list|save|delete`
  (thin over `/api/agent/*-triggers`), console Triggers view at `/triggers`
  (hooks + polls; schedules got its own `/schedules` view), `ConsoleTriggers`
  Lambda route. Tests: `tests/test_cli_triggers.py`.
- **Fresh provider token, console side** — `POST /api/admin/connections/{id}/token`
  (same `tokens.get_access_token` domain call as the CLI's `token exec|write`),
  Connections-view "Get token" dialog that shows the value once and clears it
  on close. Tests: `tests/test_admin_token_issue.py`.
- **Designer copilot** — console-mode "Copilot" dialog over
  `/api/admin/copilot/draft`: prompt → draft YAML preview (with save-time
  validation errors surfaced), "Load into canvas" for review; nothing
  auto-saves or publishes.
- **Insert from previous steps** — the designer inspector lists prior steps
  (run order, save-time ids) with their tested output keys as click-to-copy
  `{steps.<id>.output.*}` chips, so templating no longer requires hand-typed
  paths.
- **Operator search surfaces** — cross-workflow search moved server-side
  (`?q=` over id/description/trigger/action types on the designer list and
  overview routes, `dapier workflows list --search`, debounced console
  search) and workflow summaries carry `description`.
- **Alarm emails** — the five CloudWatch alarms (event/dropbox/render DLQs,
  worker, backup) had no `AlarmActions`, so fired alarms went nowhere; they
  now point at one SNS topic whose subscriber (`alarm_notify.py`) emails
  the operator address via SES, the same recipient run failures use.
- **Schedule timezones** — `cron(...)` expressions accept EventBridge's
  optional seventh timezone field (`cron(0 9 ? * MON * Europe/Berlin)`),
  passed through to PutRule untouched.
- **Audit trail, workflow export, and workflow search** — the operator-action
  audit trail (`audit.py`) became readable: `GET /api/{admin,agent}/audit`
  (operator-gated, no-store; exact-or-family `action` filters — a trailing
  dot like `workflow.` covers the family — plus connection, actor, agent,
  outcome, `since`/`before`, free-text `?q=` over the display fields, and
  keyset paging via `?next=`), a bounded CSV export at
  `/api/{admin,agent}/audit/export` that itself writes an `audit.export` row
  so bulk reads leave a mark in the trail, `dapier audit list` /
  `audit export [--out F] [--max-rows N]` mirroring every filter, an overview
  **Activity** panel and a full `/audit` console view (search, filters, Load
  more, **Export CSV**); rows are projected to exactly what the audit module
  stores, so no token or client secret can surface. Workflow round-trip is
  symmetric: `designer_store.api_get` now returns the canonical `yaml` beside
  the parsed definition (re-saving an export is byte-identical), exposed as
  `dapier workflows export <file> [-o out.yaml]` and the console's
  **Download YAML** / **Import YAML** (the import posts the existing save
  endpoint). And `?q=` search matches workflow id, description, trigger, and
  action step types on `GET /api/{admin,agent}/designer/workflows` and the
  overview endpoints (description now surfaced in list summaries and
  `_workflow_view`), wired to `dapier workflows list --search` and the
  console's workflow search box. Tests: `tests/test_audit_reader.py`,
  `tests/test_designer.py`, `tests/test_admin.py`, `tests/test_agent_api.py`,
  `tests/test_cli.py`.

Reviewed 2026-09-27 against committed HEAD (`97b2a2c`) plus the in-flight working
tree (trigger discovery and designer changes are being built separately; noted
where they land). Read alongside `docs/architecture-review.md`, which covers the
registry/execution refactor; this document covers the *product loop* gaps beyond
the discovery/replay/test work already underway.

## 1. The Zapier loop, mapped to dapier today

| Zapier concept | Dapier equivalent | Status |
| --- | --- | --- |
| Trigger connectors (webhook, email, schedule, polling) | hook/email/schedule/poll triggers (`src/dapier/triggers/`), registry chips (`connectors/registry.py`) | have |
| Trigger sample discovery ("find recent data") | `dapier connections discover` → `/api/agent/connections/{id}/discover` (`dapier_cli/commands.py:807`) | in flight |
| Action catalog with field schemas | `registry.Action.fields`, served by `GET /api/catalog` (`connectors/registry.py:146`) | have |
| Search / find-record actions (lookup, create-if-missing) | none — only append/post/send/upload/delete (`connectors/*.py:6` registrations) | **missing** |
| Multi-step workflows with data mapping | `steps` context + formatters (`engine/actions/templating.py:38`), `http_request` output capture (`connectors/webhook.py:44`) | have |
| Filters and branching (Paths) | `filter`/`condition`/n-way `paths` on the full trigger-filter operator set; rules read earlier `steps` outputs (`engine/logic.py`, `matching._matches_filter`) | have |
| Looping | `for_each` (`engine/logic.py:275`) | have |
| Delay / "schedule after" / requeue-until | `delay` sleeps in-process, capped 60 s (`engine/logic.py:42,270`) | **missing** (real delays) |
| Digests / batching | nothing (`rg -i "digest\|batch"` finds only SQS batching) | **missing** |
| Per-step error handling / on-failure action | a step error raises and fails the run (`engine/logic.py:187-193`) | **missing** |
| Retry/backoff policy per action | workflow-level `retry` redrive + per-step `autoretry` with exponential backoff (`engine/worker.py`, `engine/logic.py`) | have |
| Task history (runs, step I/O) | EXECUTIONS_TABLE + run grouping (`api/runs.py`) | have |
| Replay a task | `api_replay` re-injects the envelope onto the queue (`api/runs.py:190`) | have |
| Test before publish (dry-run/execute) | `engine/dryrun.py`, designer Test panel, `wf test` CLI | have (stale runner list, below) |
| Field mapping autofill from sample data | inspector renders static `fields`; dry-run context uses a `{"dry_run": True}` placeholder (`engine/dryrun.py:151`) | **missing** |
| Workflow versioning / rollback | `#v<n>` version records in PUBLISHED_WORKFLOWS_TABLE (`triggers/published_workflows.py`); versions list + rollback on console, CLI, and API | have |
| Find my zap (cross-workflow search) | overview lists all workflows (`api/overview.py:100`); no search/filter | **missing** |
| Task usage metering | none (`rg "usage\|meter\|quota"` finds nothing) | **missing** |
| Error alerting to the user | opt-in email per failed run (`engine/notify.py:74-79`); alarms are infra-level only (`template.yaml:596-644`) | partial |
| Save-time input validation | required/unknown keys + template syntax (`connectors/registry.py:105-133`); no field types/enums | partial |
| Sub-zaps (call another workflow) | no `run_workflow` step (`engine/logic.py` has 4 logic types) | **missing** |
| Data stores (Zapier Storage API) | nothing | **missing** |
| Outbound webhooks with signing/retry | HMAC signing (`engine/actions/webhook.py:13-15`); retry = redrive only | partial |
| Templates / shared zaps (marketplace) | shared `flows:` catalog (`engine/matching.py:42`), copilot drafts (`copilot.py`) | partial (no sharing UI) |

## 2. Ranked gaps

### G1. Find-record / search actions (effort M, value H)
Zapier's "Find X" (lookup row, create if missing) is the most-emulated action
pattern. Dapier has only create-shaped actions: `sheets_append_row`
(`engine/actions/sheets.py:81`, append-only — no `values.get`), Slack
`chat.postMessage` only (`engine/actions/slack.py:29`), no Dropbox search.
**Sketch:** add `sheets_find_row` to `engine/actions/sheets.py` — call
`values.get` via the same `transport` injection, match with
`logic.evaluate_rules` (`engine/logic.py:78`), return the row (and
`found: false`) in the step output; optional `create_if_missing: true` lets the
author branch or fall through to a templated append. Register in
`connectors/sheets.py` (catalog picks it up automatically). Storage: none (the
output rides the existing `steps` mechanism). Tests: mirror the transport-style
unit tests used for append (`tests/test_sheets_actions.py` pattern in `make
test`); API/CLI need no new endpoints — it is exercised through `wf test` and
real runs.

### G2. Real delays: requeue-until (effort M, value H)
`delay` sleeps inside the Lambda invocation and is capped at 60 s
(`engine/logic.py:42`); "delay until 3 pm" or "wait 1 day" is impossible.
**Sketch:** a continuation envelope the worker understands: `engine/worker.py`
`handler` detects a `{"dapier_resume": {workflow_id, event, pending_steps,
step_outputs, resume_at}}` record and calls `run_chain` on the remaining steps;
the `delay` step (`engine/logic.py:_run_delay`) enqueues that envelope onto
`EVENT_QUEUE_URL` with SQS `DelaySeconds=min(seconds, 900)` (SQS supports 15
min with no infra change; longer waits chain re-enqueues or accept a documented
cap). The parked step is written to EXECUTIONS_TABLE as `waiting` so run
history shows it. Tests: `tests/test_worker.py` resume path; `tests/test_logic.py`
delay split at the 60 s boundary.

### G3. Per-step error handling and on-failure actions (effort M, value H)
Today any step exception aborts the run (`engine/logic.py:187-193`); there is
no `continue_on_error`, no error branch, no per-action retry count — only the
global redrive. **Sketch:** generic step keys `on_error: halt|continue|run` and
`error_actions: [...]`. `engine/logic.py:_run_step` catches, and on
`continue` marks the step `failed` and proceeds; on `run` it executes
`error_actions` through the same `run_chain` (prefix `<step>.error`) with
`{steps.<id>.error}` templatable. `connectors/registry.py:126` allows the new
keys. Tests: `tests/test_logic.py` for all three modes + error-branch telemetry
hooks.

### G4. Task usage metering (effort S, value M)
Zapier's core plan primitive (tasks/month) has no equivalent: no counter
anywhere (`rg "usage|meter|quota"` — no hits), `overview()` returns raw 25-item
scans (`api/overview.py:100-113`). **Sketch:** in `engine/worker.py`'s
`after_action` path, `UpdateItem ADD tasks :1` on a `TASK_USAGE` item
`{workflow_id, yyyymm}` (today granularity via a `day` attribute if wanted).
Endpoints: `GET /api/admin/usage?months=12` and `GET /api/agent/usage` for CLI
parity; `overview()` embeds a `usage` block; CLI `dapier usage`. Tests:
`tests/test_usage.py` covering the rollup and both endpoints.

### G5. Cross-workflow search + run filters ("find my zap") (effort S, value M)
`runs.api_list` accepts only `limit` (`api/runs.py:118-120`) — no filter by
workflow, status, or date; the console's runs view filters client-side over the
same unfiltered 25. Workflows list has no query at all. **Sketch:** accept
`workflow_id`, `status`, `since` on `GET /api/admin/runs` and
`GET /api/agent/runs` (`api/runs.py:api_list` gains a filter dict; a
workflow_id GSI on EXECUTIONS_TABLE or a bounded `scan` FilterExpression in the
current style); CLI `dapier runs list --workflow x --status failed`; console
`views/runs.js` moves filtering server-side. Tests: `tests/test_runs.py`.

### G6. Workflow versioning and rollback (effort M, value H)
Saves publish instantly (`api/designer_store.py:8-13`) and the git commit is
the only version record — there is no versions list, no diff, no rollback
endpoint; PUBLISHED_WORKFLOWS_TABLE holds one item per id
(`triggers/published_workflows.py` overlay). A bad save is live until a human
fixes it. **Sketch:** stamp each published item with `revision` (increment) and
keep the previous item under `id#v{n}` in the same table; endpoints
`GET /api/admin/designer/workflows/{id}/versions` and
`POST .../rollback` (re-publish old revision + commit YAML, mirroring save);
CLI `dapier workflows versions <id>` / `dapier workflows rollback <id> [rev]`.
Tests: `tests/test_designer.py` rollback round-trip and enable-flag semantics.

**Shipped:** every publish (save, toggle, duplicate-copies keep their own
history, rollback) stamps the live item with a monotonic `revision` and
appends a `test-flow#v<n>` record — definition, operator, timestamp, and
cause — pruned to the 20 most recent (`triggers/published_workflows.py`,
MAX_VERSIONS). Version records share the table but are filtered out of the
engine merge and every list. `GET …/{file}/versions` lists a workflow's
history newest-first with the live revision flagged; `POST …/{file}/rollback`
re-commits the old YAML through the save path (so git and live agree) and
publishes it as the next revision with `cause: "rollback"` — an omitted
revision restores the one before the live version (409 at v1). Wired on all
three surfaces: console Versions dialog (`views/overview.js`), CLI
`dapier workflows versions|rollback`, and the same store behind both
`/api/admin/*` and `/api/agent/*` routes.

### G7. Default-on error visibility (effort S, value H)
Failure email only fires when the workflow opted in with a top-level `notify:`
list (`engine/notify.py:77-79`), and ingress failures never notify at all —
`worker.handler` passes `notify_failure(exc, None)` for poll triggers
(`engine/worker.py:176-178`), and `notify.py:71-73` returns None for any
non-dict event. CloudWatch alarms cover DLQs/worker errors only
(`template.yaml:596-644`). **Sketch:** default `notify` to the operator address
(DAOPIER_EMAIL_SENDER domain) unless explicitly `notify: []`; add
`GET /api/admin/errors/summary` (failed-run counts by workflow from the
existing runs scan) and a failed-run badge in `views/overview.js`; CLI
`dapier runs list --status failed` (G5). Tests: `tests/test_notify.py` default
address + poll-failure path.

### G8. n-way paths (beyond then/else) (effort S after G3, value M)
Zapier Paths support named, ordered, mutually-exclusive branches. `condition`
is binary with implicit "both run" semantics for `else` omitted
(`engine/logic.py:243-261`). **Sketch:** a `paths` logic step: list of
`{label, when, actions}` evaluated in order, first match wins, optional
`default`. Reuses `run_chain` recursion; validation in
`api/designer_store.py` bounds. Tests: `tests/test_logic.py` first-match and
no-match behavior.

### G9. Trigger field mapping / autofill UX (effort M, value M, depends on discovery)
The inspector renders static `fields` from the catalog
(`connectors/registry.py:36`); nothing tells the author what the trigger data
looks like, and dry-run confirms only that templates parse
(`engine/dryrun.py:151` uses a `{"dry_run": True}` placeholder). **Sketch:**
`GET /api/admin/triggers/sample?workflow=<id>` returning the most recent run's
recorded `input` (query EXECUTIONS_TABLE by workflow via the G5 index, falling
back to discovery resources); the designer inspector offers insertable
`{paths...}` chips from that sample; `execute_once` then feeds a *real*
envelope. Tests: `tests/test_designer.py` sample fetch; manual UI check per
`AGENTS.md` parity audit.

### G10. Save-time input validation with types (effort S, value M)
Validation checks required keys, unknown keys, and template syntax
(`connectors/registry.py:105-133`) but not types or enums: `timeout_seconds`
could be `"abc"`, and a missing templated field silently renders as `""`
(`engine/actions/templating.py:128-131`) — Zapier flags empty required fields
instead of sending blanks. **Sketch:** `fields` entries carry `type: number|email|url`
and `required: true`; `validate_action_chain` enforces them and warns (or
fails under `strict: true`) when a required-by-connector template renders
empty against a sample. Tests: `tests/test_registry.py` + `tests/test_designer.py`.

### G11. Digest / batching step (effort M, value M, needs G2)
No digest concept exists (`rg -i digest` — nothing). **Sketch:** a `digest`
logic step writing `{key, items[]}` to a DIGESTS table and flushing on a
schedule trigger within the same workflow (window from the schedule); flush
renders the accumulated list as `{digest.items}`. Depends on G2 only for
long windows. Tests: `tests/test_digest.py` accumulate/flush/idempotent claim.

### G12. Data stores (effort M, value M)
Zapier Storage (key-value with search) has no equivalent; workflows that need
cross-run state (dedupe, counters, "first time I saw this sender") cannot do
it. **Sketch:** one DynamoDB table STORAGE `{owner, key, value, expires}` +
registry actions `storage_get` / `storage_set` / `storage_find` (search by
value prefix) exposed through `/api/catalog`; CLI `dapier storage get|set|find`
for parity; outputs land in `steps` like any action. Tests: `tests/test_storage_actions.py`.

### G13. Sub-workflows (effort M, value L→M)
No step can invoke another workflow (`engine/logic.py:205-223` has only
filter/condition/delay/for_each); shared `flows:` cover code reuse but not
"trigger this other zap". **Sketch:** a `run_workflow` connector action that
calls `execute(event, workflows=[target])` in-process with a depth counter
(`max_depth: 2`), reusing the exact restriction `dryrun.execute_once` already
builds (`engine/dryrun.py:166-171`). Tests: `tests/test_engine.py` chained-run
telemetry and depth cap.

## 3. Top 3 quick wins

1. **Run filters + usage metering (G4 + G5, both S).** Highest "feels like a
   product" per line of code: operators immediately get "show me this
   workflow's failures" and "tasks used this month". Build order:
   `api/runs.py:api_list` gains filters (scan FilterExpression first — no
   migration); pass query params through `api/agent.py` `/api/agent/runs`
   (line 417) and `api/admin/routes.py` runs handler (line 21); CLI flags in
   `dapier_cli/main.py` (`runs list`) + `dapier_cli/commands.py`; then the
   TASK_USAGE rollup in `engine/worker.py` `_mark_completed` path, the two
   `/usage` endpoints, `overview.py` block, and `dapier usage`. Tests:
   extend `tests/test_runs.py`, new `tests/test_usage.py`.

2. **Registry-truth dry-run (bug fix riding G10, S).** `engine/dryrun.py:30-39`
   still hardcodes the pre-registry 8 runner types, so a dry-run of any
   `code`, `http_request`, `s3_upload`, or `sheets_append_row` step — 4 of the
   12 registered actions — reports "unsupported action" for workflows that run
   fine in production (`dryrun.py:143-145`). That poisons trust in the Test
   panel the team is actively building. Build order: replace RUNNER_TYPES with
   a lookup into `connectors.registry.ACTIONS` (registry never imports the
   engine, so the import stays lazy/literal as it does in
   `registry.validate_action_chain`); while there, apply the G10 field-type
   checks. Tests: `tests/test_dryrun.py` cases for each of the 12 types.

3. **Sheets find-row with create-if-missing (G1, S/M).** The emblematic Zapier
   action, and the plumbing (transport injection, `steps` outputs, catalog
   fields) already exists — it is mostly one module. Build order:
   `run_sheets_find_row` in `engine/actions/sheets.py` next to `_append_rows`
   (reuse `values.get` + `transport`), register in `connectors/sheets.py` with
   fields (`spreadsheet_id`, `sheet_name`, `match_field`, `match_value`,
   `create_if_missing`); document the `{steps.find.output.*}` pattern in
   `docs/connectors/google.md`; verify through `wf test` and a live workflow
   rather than new endpoints (parity holds: console designer, CLI `wf test`,
   and runs views all reach it through the same API). Tests:
   `tests/test_sheets_actions.py` found/not-found/create-if-missing.

Immediate follow-up: G2 (requeue-until) — it is the prerequisite for digests
(G11) and unlocks "wait until date" workflows, and it needs the resume
envelope in `engine/worker.py` designed before anyone adds more in-process
sleep-based behavior.

## Surprises found while auditing

- **`engine/dryrun.py:30` RUNNER_TYPES is stale** — 8 of 12 registered actions
  fail dry-run with "unsupported action" (quick win 2).
- **A redelivery arriving while a step's 300 s lease is live silently skips
  that step** and continues the chain: `_is_pending` returns False on the
  conditional-write conflict (`engine/worker.py:83-85`) and `run_chain` marks
  it `skipped` (`engine/logic.py:176-178`) — a stuck step can be passed over
  rather than retried, with only run history to show for it.
- **`webhook` (the plain action) discards the response body**; only
  `http_request` captures it (`connectors/webhook.py:44-50`), so signed
  webhook users cannot chain on responses.
- **Poll-trigger failures never notify anyone** (`engine/worker.py:176-178`
  passes `event=None`, which `notify.py:71-73` declines).

## Round 3 (2026-09-28): every provider can discover, test, replay

The last connector-level hole is closed: **Mailchimp** is now a real
connector, not just a credential spec —
`mailchimp_find_member` / `mailchimp_upsert_member` actions,
`mailchimp.audiences` / `mailchimp.members` discovery, and a ping health
check, all behind the stored API key and reachable through the synthetic
"mailchimp" connection (`api.discovery.PSEUDO_CONNECTION_PROVIDERS`), so
`dapier connections discover|test mailchimp` and the designer's audience
picker work like every other provider. Designer palette entries +
`make designer-console` rebuild; console Credentials view grew a **Test**
button for credential-backed providers (mailchimp, aws) hitting the same
`POST /api/admin/connections/{provider}/test`. Tests:
`tests/test_discovery_mailchimp.py`.

Also landed from the 2026-09-28 audit: **digest steps no longer fail
"Test step" as unsupported** (`dryrun._evaluate_logic_step` evaluates
mode/key and peeks the pending count), and **11 new formatters** —
`split, join, title, urlencode, length, truncate, slugify, add, subtract,
multiply, divide` (`engine/actions/templating.py`).

### Open findings from the 2026-09-28 product-loop audit (ranked)

1. **Replay always reruns the whole workflow** — no replay-from-failed-step
   (`runs.py` `api_replay`); a targeted resume can seed the steps context
   from the stored run (M).
2. **Run history has no content search** — `_wanted` filters by
   workflow/status/time only; `q` matching over step input/output JSON
   inside the existing scan window would answer "which run carried order
   #1234" (M).
3. **Connection listings show no token expiry / reconnect flag** —
   `public_view` exposes status only; the console's `expired → needs
   reconnection` mapping is dead code because nothing writes that status.
   Enrich the list with `token_expires_at` + offline `health: ok|expired`
   (S).
4. **No scheduled daily operator error digest** — `errors.api_summary` is
   on-demand only; EventBridge rule → SES render of the summary, plus a
   send-now endpoint for parity (S/M).
5. **Workflows cannot be deleted on any surface** — only disable;
   unpublish + one atomic git tree-delete commit, refusing while runs are
   delayed (M).
6. **Webhook trigger response is a fixed 202 ack** — no sync-response
   option for challenge/interactivity callers; needs inline execution when
   `response.sync` is set (M/L).
7. **Dedupe gaps** — `next_cursor` polls treat every page as new; webhook
   retries double-run (fresh uuid per POST). A seen-item TTL set for polls
   and an optional `dedupe_path` for hooks close it (S/M).
8. **Workflow organization: search yes, tags/folders/bulk-toggle no**
   (M).

