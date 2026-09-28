# Zapier gap analysis: what dapier still lacks

## Landed since this analysis (2026-09-27)

- **Options discovery on every resource field** — the last fields that named
  a provider resource with no picker now browse live: a param-free
  `youtube.videos` discovery (the channel's uploads via
  `contentDetails.relatedPlaylists`) backs `video_id` on
  `youtube_add_to_playlist`/`youtube_update_video`, and Slack's `messages`
  resource backs `thread_ts` on message/schedule/upload plus `ts`/`timestamp`
  on update/reaction (channel param resolved from the sibling field). Mirror
  pins in `tests/test_designer_pickers.py` keep `catalog.ts` and the registry
  hints locked together; discovery behavior in `tests/test_discovery_youtube.py`.
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
  the storage actions use — arrival-ordered keys, 500 pending items max —
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

### Landed 2026-09-28 (round 5: provider poll sources — every chip can fire, discover, replay, test)

- **Pluggable poll sources** — a stored poll trigger's `source` key swaps the
HTTP+JSON fetcher for a registered provider fetch (`triggers/poll_sources.py`)
on the same EventBridge schedule, cursor and seen-store dedupe. The
published events carry the source's connector/event names, which makes the
provider chips real triggers; the poll-name filter still scopes each
trigger's runs. `build_item` merges each source's params (bucket,
spreadsheet_id, folder_id, ...) under the item's locked identity keys and
validates a source's cursor defaults with the generic checks; an unknown
source fails the save with the valid choices. Tests:
`tests/test_poll_sources_seam.py`.
- **`s3` (`file.created`)** — boto3 ListObjectsV2 on `bucket` (+ optional
`prefix`, `credential_id` defaulting to the shared `aws` credential).
Composite ids (`last_modified|key`) keep same-second objects distinct;
first fire seeds the cursor without emitting, later fires get new objects
only, oldest first. Tests: `tests/test_s3_trigger.py`.
- **`google-sheets.rows` (`row.new`)** — values.get through the connection's
refreshed OAuth token (spreadsheet_id + optional worksheet required). Row
numbers are ids; the known polling caveat (deleted rows shift numbering)
is documented in the source. Tests: `tests/test_google_triggers.py`.
- **`google-drive.files` (`file.created`)** — files.list in a folder
(folder_id + connection required), createdTime watermark, new files only.
- **Palette + surfaces** — `google-sheets`, `google-drive`, `s3` chips
(registry + designer mirror, rebuilt); sample discovery for each follows
live (named stored poll) → history → synthetic; console Triggers view has
a Source select and shows the provider target; `dapier polls save` takes
`source` through the same body and `polls list` prints the provider
target. Replay/test coverage stays platform-level (replay-from-step, Test
step, connection tests already cover every connector).
- **Action breadth** — `zoom_create_meeting` (scheduled when start_time
renders, instant without), `dropbox_read_file` (stages file bytes for
`s3_upload` `source_s3`), `dropbox_get_temp_link` (direct download link
for `s3_upload` `source_url`). Tests: `tests/test_action_breadth.py`.
- **Media actions (2026-09-28)** — `slack_upload_file` sends one file to a
channel through the v2 upload flow (`files.getUploadURLExternal` → binary
POST → `files.completeUploadExternal`), sourced like the uploads
(`source_url` / `source_s3` / inline `content`), with title, comment and
`thread_ts`; `drive_read_file` downloads one Drive file and stages it
like `dropbox_read_file`, exporting Google-native docs first
(`export_as`; document/presentation → PDF, spreadsheet → CSV, drawing →
PNG by default). The chain dropbox/drive read → slack upload moves bytes
end-to-end. Tests: `tests/test_slack_upload_file.py`,
`tests/test_drive_read_file.py`.
### Landed 2026-09-28 (round 6: options discovery everywhere, quick actions, new trigger events)

- **Options discovery for every listing** — every connection-scoped
`Discovery` in the registry now has a `kind=options` twin in the
trigger-discovery layer, so `POST /api/{agent,admin}/discover` can fill
any field without typing ids: added `google-sheets.worksheets|columns|rows`,
`google-drive.folders`, `zoom.recordings`, `slack.users|messages`,
`mailchimp.members`, `dropbox.files|search`,
`youtube.channel|playlist_items|videos`. Listing arguments ride the
request's `event` slot, generalized as `trigger_discovery.listing_params`
(one key = the whole value; several keys = "/"-positional with provider
defaults; a missing required value is a 404, never a fabricated listing).
Stored-trigger pickers: `poll.triggers` and `schedule.triggers` list the
stored names, so a live sample needs no typing. An invariant test pins
the registry↔options set equality so this cannot drift again. Tests:
`tests/test_options_breadth.py` (plus the webhook/email/dataops
sample-payload contracts the audit found untested).
- **Quick actions** — `drive_move_file` (add/remove parents via
`files.update`), `drive_delete_file` gained `permanent` (trash default),
`sheets_add_worksheet` (batchUpdate addSheet), `zoom_delete_recording`
(trash default), `s3_list_objects` (bounded listing with
`truncated`/`next_token`), and email threading (`in_reply_to`/
`references` on `email_send`; sends carrying them ride the raw-MIME
path, since SES simple sends cannot carry custom headers). Catalog
mirror synced (`catalog.ts`), including the pre-existing
`sheets_clear_values` drift. Tests: `tests/test_quick_actions.py`.
- **New trigger events** — telegram `channel_post.received` (incl.
`edited_channel_post`) now flows as a distinct event through the
telegram hook (`hook_triggers.telegram_event_for`) with per-event
discovery samples and the same filter matching; zoom
`meeting.registration_created` is mapped in the webhook intake with its
own sample and per-registrant dedupe. Tests:
`tests/test_trigger_events_breadth.py`, updates to
`tests/test_zoom.py`/`tests/test_hook_triggers.py`/
`tests/test_trigger_samples.py`.
- **Designer duplicate-step hardening** — Cmd/Ctrl+D duplicate (context
menu + inspector) gained the contenteditable focus guard and always
preventDefaults in canvas view. Presentational only (parity exception 5).

Still open after round 6 (deliberately deferred): plan/quota *enforcement*
on top of usage metering; multi-user workspaces (and with it template
cross-account transfer); new app connectors beyond the current 14 chips
(Gmail, Google Calendar, …); Google Sheets `row.updated` (needs snapshot
diffing); an SES bounce/complaint trigger chip.

### Landed 2026-09-28 (round 7: test-trigger in the console, live workflow samples, thread/poll/participant staples)

- **The console can configure all ten poll sources** — the Triggers
  dialog's Source select hardcoded six of them; google-drive.updates,
  google-drive.deletions, mailchimp.members and slack.messages were
  reachable only by hand-writing the Options JSON. The select now lists
  every registered source, `CONNECTION_POLL_SOURCES` gained
  slack.messages (the connection-required early check), and Mailchimp and
  Slack targets (`list_id`, `channel_id`) show in the Watches column. A
  drift gate (`tests/test_console_trigger_sources.py`) pins both
  hardcoded lists to `poll_sources.source_names()` so the next source
  cannot land without its console surface.
- **"Test trigger" in the console** — the Triggers view's poll rows have a
  **Sample** button: one `POST /api/admin/discover` with the generic poll
  connector live-fetches the stored poll's own page (HTTP or provider
  source) without advancing its cursor and shows the envelope exactly as
  a real fire publishes it — Zapier's "we found a sample" moment, which
  until now needed the designer or the CLI. Read-only, so no CLI
  counterpart is required (`dapier triggers sample` already drives the
  same domain dispatch).
- **Samples go live for workflow-bound polls** — `api_trigger_sample`'s
  fallback passed the trigger's *event name* where every provider's live
  branch keys on the stored poll *name*, so a never-run workflow with a
  perfectly good bucket/sheet/channel to pull always got the documented
  example. The sample now resolves the stored poll bound to the workflow
  (`flow:`), matches it against the workflow's connector, and the
  designer test panel and `dapier triggers sample --workflow` get live
  data off the real source; a bound poll serving another connector is
  ignored. Tests: `tests/test_trigger_sample.py`.
- **Field-options listings take their parameters in the console** — the
  sample dialog rendered no inputs for param-requiring option resources
  (s3.objects needs a bucket, worksheets a spreadsheet), so picking one
  404ed. The dialog now renders the resource's param fields from the
  catalog's discovery metadata and passes them through the event slot's
  positional convention (`listing_params`) — the same convention
  `dapier triggers sample --event` uses.
- **Action staples**: `slack` gains `thread_ts` (reply in thread to the
  triggering message — the most common Slack pattern) and
  `slack_schedule_message` (chat.scheduleMessage; ISO-or-epoch `post_at`,
  thread-aware); `telegram_send_poll` (one option per line, 2–10,
  chat fallback); `drive_find_file` returns the bounded `files` list +
  `next_page_token` beside its back-compat keys; `sheets_clear_values`
  (values:clear — blank cells without shifting rows);
  `s3_find` walks its listing — `matches` (≤25) and `next_token` in and
  out, `found`/`key` untouched; `zoom_list_past_participants` (paginated,
  capped at 300) pairs with the meeting.ended trigger for the
  attendance-to-sheet automation. All registered with designer mirror
  entries (rebuilt) and unit tests in `tests/test_round_threadspoll.py`,
  `tests/test_round_drivefolders.py`, `tests/test_round_s3zoom_listing.py`.

Reviewed 2026-09-27 against committed HEAD (`97b2a2c`) plus the in-flight working
tree (trigger discovery and designer changes are being built separately; noted
where they land). Read alongside `docs/architecture-review.md`, which covers the
registry/execution refactor; this document covers the *product loop* gaps beyond
the discovery/replay/test work already underway.

## 1. The Zapier loop, mapped to dapier today

| Zapier concept | Dapier equivalent | Status |
| --- | --- | --- |
| Trigger connectors (webhook, email, schedule, polling) | hook/email/schedule/poll triggers (`src/dapier/triggers/`), registry chips (`connectors/registry.py`) | have |
| Trigger sample discovery ("find recent data") | sample for every chip (`connectors/trigger_discovery.py`); options discovery for every registry listing, invariant-tested (`tests/test_options_breadth.py`); dispatch shared by `/api/{agent,admin}/discover` | have |
| Action catalog with field schemas | `registry.Action.fields`, served by `GET /api/catalog` (`connectors/registry.py:146`) | have |
| Search / find-record actions (lookup, create-if-missing) | 14 find actions with `create_if_missing` (e.g. `sheets_find_row`, `dropbox_find`, `slack_find`, `zoom_find_meeting`, `mailchimp_find_member`) | have |
| Multi-step workflows with data mapping | `steps` context + formatters (`engine/actions/templating.py:38`), `http_request` output capture (`connectors/webhook.py:44`) | have |
| Filters and branching (Paths) | `filter`/`condition`/n-way `paths` on the full trigger-filter operator set; rules read earlier `steps` outputs (`engine/logic.py`, `matching._matches_filter`) | have |
| Looping | `for_each` (`engine/logic.py:275`) | have |
| Delay / "schedule after" / requeue-until | `RunSuspended` requeue-until (`engine/logic.py` `_run_delay`, `engine/worker.py` `_resume_run`) | have |
| Digests / batching | `digest_add`/`digest_flush` + `digest` logic step (`connectors/digest.py`, `engine/actions/digests.py`) | have |
| Per-step error handling / on-failure action | `on_error`/`on_fail` (`engine/logic.py` `_handle_error`, `connectors/registry.py` validation) | have |
| Retry/backoff policy per action | workflow-level `retry` redrive + per-step `autoretry` with exponential backoff (`engine/worker.py`, `engine/logic.py`) | have |
| Task history (runs, step I/O) | EXECUTIONS_TABLE + run grouping (`api/runs.py`) | have |
| Replay a task | `api_replay` re-injects the envelope onto the queue (`api/runs.py:190`) | have |
| Test before publish (dry-run/execute) | `engine/dryrun.py`, designer Test panel, `wf test` CLI | have (stale runner list, below) |
| Field mapping autofill from sample data | trigger-sample endpoint feeds the inspector and Test step (`api/runs.py` `api_trigger_sample`, `engine/dryrun.py` `test_step`) | have |
| Workflow versioning / rollback | `#v<n>` version records in PUBLISHED_WORKFLOWS_TABLE (`triggers/published_workflows.py`); versions list + rollback on console, CLI, and API | have |
| Find my zap (cross-workflow search) | `?q=` search over workflows and run content (`api/overview.py` `_workflow_matches`, `api/runs.py` `api_list`) | have |
| Task usage metering | task counting + monthly rollup (`engine/usage.py`, `dapier usage`); no plan caps yet | have (metering; caps open) |
| Error alerting to the user | opt-in email per failed run + error digest (`engine/notify.py`, `error_digest.py`) | have |
| Save-time input validation | required/unknown keys, template syntax, field types (`connectors/registry.py` `validate_action_chain`, `validate_field_types`) | have |
| Sub-zaps (call another workflow) | `run_workflow` step (`connectors/subworkflow.py`) | have |
| Data stores (Zapier Storage API) | `storage_get/set/delete/find` + storage API + console view (`connectors/storage.py`, `api/storage.py`, `views/storage.js`) | have |
| Outbound webhooks with signing/retry | HMAC signing + typed `HttpError` + per-step `autoretry` (`engine/actions/webhook.py`, `engine/logic.py`) | have |
| Templates / shared zaps (marketplace) | `template: true` flag rides the workflow YAML; gallery + apply (fork) + publish/unpublish on console (designer), CLI (`dapier templates`), and API (`/designer/templates*`); bundled starters in `workflows/template-*.yaml` | have (v1: single-tenant gallery; cross-account transfer still needs multi-user) |

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

### Connector coverage matrix (2026-09-28 audit)

Every capability cell is backed by a registry entry, verified by importing
`connectors` and dumping `registry.catalog()` +
`trigger_discovery.trigger_discovery_catalog()`. "—" means genuinely
not-applicable (no connection record, no remote listing), not missing.

Trigger connectors (palette chips + sample pull):

| connector | events | sample pull | trigger options | action pickers | conn test | find actions |
|---|---|---|---|---|---|---|
| email | message.received | ✓ | — | — | — (no connection record) | — (send only) |
| youtube | video.published | ✓ | — (push, no trigger fields) | playlists | youtube | find video, find playlist videos, upload video (needs `youtube.upload` scope — see docs/connectors/google.md) |
| dropbox | file.created/updated/deleted | ✓ per event | folders | files, folders, search | dropbox | find file or folder |
| zoom | recording.completed, recording.transcript_completed, meeting.started, meeting.ended | ✓ per event | meetings | meetings, recordings | zoom | find meeting, find recording |
| slack | message.received, app.mention, reaction.added, member.joined | ✓ per event | channels | channels, users, messages | slack | find user or channel (create-if-missing on channels), find user by email, find message, update message, add reaction, pin, invite users, DM, create channel, set topic/purpose, upload file, schedule, add reminder |
| telegram | message.received, channel_post.received | ✓ per event | chats | chats | telegram | send, send photo, send document, send poll, find chat |
| renderer | job.completed | ✓ | — | — | — (internal) | — |
| schedule | schedule.triggered | ✓ | — | — | — | — |
| poll | item.new | ✓ | — | — | — | — |
| custom (webhook ingress) | freeform | ✓ | — | — | — | — |

Action-only providers (credential-backed, no trigger chip):

| provider | action pickers | conn test | find actions |
|---|---|---|---|
| google (Sheets + Drive) | spreadsheets, worksheets, columns, rows, files, folders | google | find/lookup/update row, find file |
| s3 / aws keys | buckets, objects | aws | find object |
| mailchimp | audiences, members | mailchimp | find member, upsert member |

Everything else (code/js, logic steps, storage, digest, delay, webhook and
http actions, run_workflow) is internal or generic and needs no remote
listing; `dataops` is action-side intake, so its sample pull is for test
panels only and it deliberately has no palette chip.

One hole the audit closed: **telegram had no palette chip** — its hook
trigger, sample pull, chat options, connection test, and find-chat action
all existed, but `CONNECTORS` never listed it, so the designer's trigger
palette and `GET /api/catalog` had no Telegram entry. Added to
`connectors/triggers.py` (`message.received`, like the hook engine's
`TELEGRAM_EVENT`); `tests/test_trigger_samples.py` now asserts every
sample-discoverable connector has a chip. The webhook-ingress sample stays
keyed `webhook` — it surfaces as the Custom chip.

### Open findings from the 2026-09-28 product-loop audit (ranked)

1. **CLOSED (2026-09-28)** — replay-from-step: `runs.api_replay(run_id,
   from_step=<top-level step id>)` reuses the worker's own park-and-continue
   path — a synthetic `dapier_resume` envelope carries the workflow's tail
   from the chosen step plus the recorded outputs of everything before it
   (seeded `step_outputs`, so templates still read `{steps.<id>.output.*}`),
   and a fresh event id lands the rerun in history tied to the original by
   `correlation_id`. Full replay (re-injected trigger) stays the default;
   the early steps never re-fire on a targeted retry. On every surface:
   `POST /api/{admin,agent}/runs/{id}/replay` with a `from_step` body
   (audited as `runs.replay-from-step`), `dapier runs replay --from-step`,
   and a per-step "Replay from here" button in the console run dialog.
   Refusals are explicit: workflow gone (404), disabled (409), a step name
   nothing records or defines (404), or a recorded step that is not a
   top-level step of the current definition (409). Tests:
   `tests/test_runs.py`, `tests/test_agent_api.py`, `tests/test_admin.py`,
   `tests/test_cli.py`.
2. **CLOSED (2026-09-28)** — run-history content search: `?q=` on
   `GET /api/{admin,agent}/runs` does a case-insensitive substring match
   over each run's recorded step data (input, output, error) and ids inside
   the same bounded scan window the list already walks, so "which run
   carried order #1234" answers from history; composes with the
   workflow/status/time filters and keyset paging, and `paging.filtered`
   stays honest. Wired to `dapier runs list --search` and the console
   Runs view's debounced search box. Tests: `tests/test_runs.py`,
   `tests/test_agent_api.py`, `tests/test_cli.py`.
3. **CLOSED (2026-09-28)** — listings carry an offline `health:
   ok|expired` + `token_expires_at` computed from the stored OAuth token
   (`records.public_view(item, stored)`, fed by `tokens.stored_value` on
   `/api/agent/connections` and `/api/admin/overview`). The console
   Connections view turns expired tokens into a live "needs reconnection"
   row with a Reconnect action — counts, filter, and sort follow — and
   `dapier connections list|show` print HEALTH/EXPIRES. Tests:
   `tests/test_agent_api.py`, `tests/test_admin.py`, `tests/test_cli.py`.
4. **CLOSED (2026-09-28)** — the daily operator error digest is scheduled
   (`ErrorDigestFunction`, cron 07:00 UTC): `error_digest.py` renders the
   `api_summary` rollup into one SES email to the operator address, a
   zero-failure window skips the send, and function errors page the alarm
   chain (`ErrorDigestErrorAlarm`). Send-now parity: operator-gated
   `POST /api/{admin,agent}/errors/digest` (audited `errors.send-digest`
   on real sends), `dapier errors send-digest`, and the console
   Failed-runs panel's "Send digest now". Tests: `tests/test_error_digest.py`.
5. **CLOSED (2026-09-28)** — workflow delete on every surface:
   `designer_store.api_delete` unpublishes the live item (the engine stops
   matching on the next event), then removes `workflows/<file>` in one
   atomic git tree-delete commit (`commit_delete`, a `sha: null` entry
   against the base tree — the same mechanism a rename uses), refusing 409
   while any run is parked on a delay (a resume would dangle into a
   definition that no longer exists; the operator cancels or waits them
   out). Run history is untouched — deleting stops the automation, not the
   audit trail. A git failure after a live delete reports
   `git_sync_error` and still counts as deleted. Surfaces: audited
   `DELETE /api/{admin,agent}/designer/workflows/<file>` (console Delete
   button with confirm), `dapier workflows delete [--yes]` (warns when the
   commit failed). Tests: `tests/test_workflow_delete_tags.py`.
6. **CLOSED (2026-09-28)** — webhook triggers answer sync: `response: {mode:
   ack|challenge|sync, template}` on the stored hook (webhook kind only —
   Telegram keeps its fast fixed ack), validated at save on both dispatchers
   through `build_item`. `ack` (the default) is the old fixed 202;
   `challenge` answers verification handshakes (query `challenge` /
   `hub.challenge`, falling back to the body's `challenge`) with plain-text
   200 and runs nothing; `sync` runs the matched workflow inline in the
   ingress — the production path (step leases, run history, task usage,
   failure notify, workflow retry) via `worker.execute` with the worker's own
   attempt hooks — and answers 200 with the `template` rendered against
   `{trigger.*}` / `{steps.<id>.output.*}` (any JSON shape; default body
   `{"ok", "workflow", "steps", "event_id"}`), 202 `{"suspended": true,
   "resume_at": …}` when a delay parks the run (the continuation is enqueued
   exactly as the worker parks it), or 500 with the delivery's dedupe claim
   released (a provider retry re-executes) when the chain fails. Optional
   `status` (2xx, default 200) sets the success code and `budget_seconds`
   (1-25, default 10) the inline run's wall-clock budget. Dedupe composes: a
   retried delivery is answered, not re-run. Sync is a single-workflow
   contract — zero or several matching workflows fall back to the ack path
   (202, with a `reason`) — and the budget is enforced between steps, never
   mid-step: on overrun the un-run tail is enqueued under the same event id
   (the completed steps' leases dedupe the worker's replay) and the caller
   gets 202 `{"reason": "budget"}`, so the run still finishes out of band;
   longer chains belong in ack mode. Surfaces: the console hook dialog's
   Response select + template JSON field, `dapier hooks save` body files
   plus `--sync-response` (shown by `hooks show`). Tests:
   `tests/test_hook_sync_response.py`, `tests/test_sync_response.py`,
   `tests/test_webhook_sync.py`.
7. **CLOSED (2026-09-28)** — trigger dedupe: a TTL-bounded seen-id store
   (`triggers/seen.py`, one item per scope in the cursors table, 30-day
   expiry, 1000-entry cap, atomic `claim`/`forget`) closes both halves.
   `next_cursor` polls now skip items whose stable id was already published
   (providers recycle pages) and park the provider's continuation cursor —
   never an item id — once a page drained; watermark polls are unchanged
   (the cursor already dedupes). Webhook triggers take an optional
   `dedupe_path` (dot-path to a stable delivery id in the payload; console
   Triggers form and CLI `hooks save` both set it): a retried delivery
   claims the same event id, answers `202 duplicate`, and never publishes —
   a publish failure releases the claim so the retry still delivers, and a
   seen-store outage publishes anyway (the stable id still dedupes
   downstream through run grouping and step leases). Telegram triggers
   dedupe by default on `update_id`, the provider's own delivery identity.
   Tests: `tests/test_seen_store.py`, `tests/test_poll_dedupe.py`,
   `tests/test_poll_triggers.py`, `tests/test_hook_dedupe.py`.
8. **CLOSED (2026-09-28)** — tags: a workflow carries up to 20
   short labels in its YAML (`tags:`, validated by `_validate_tags`), set
   through `PUT /api/{admin,agent}/designer/workflows/<file>/tags`
   (audited `workflow.tags`, published with cause "tags", committed
   best-effort like the toggle), `dapier workflows tags <file> --tags
   a,b | --clear`, and a console Tags prompt per row. Tags are normalized
   to lowercase, deduped case-insensitively, and bounded (20 × 64 chars).
   Both list endpoints narrow with `?tag=` (case-insensitive) — the CLI
   mirrors it with `workflows list --tag`, and the console's Workflows
   view gets a tag dropdown plus tag chips per row. Tags travel with
   saves, deploys, duplicates, and rollbacks because they live in the
   definition. Bulk enable/disable is also in: audited
   `POST /api/{admin,agent}/designer/workflows/bulk`
   (`{"ids": [...], "action": "enable|disable"}` with per-id results,
   refusing parked-delay workflows like the single toggle) and
   `dapier workflows on|off <file>...` taking multiple files in one call.
   And folders close the slice: a flat `folder:` top-level YAML key
   (`_validate_folder` — ≤64 chars, never a path; Zapier folders are
   flat), `PUT /api/{admin,agent}/designer/workflows/<file>/folder`
   (audited `workflow.folder`, cause "folder"), `dapier workflows folder
   <file> --set "Name" | --clear` and `workflows list --folder`, `?folder=`
   on both list routes plus the overview payload (a `workflow_folders`
   aggregate beside the tags one), and a console Folder dropdown + folder
   chips + per-row Folder prompt. Tests: `tests/test_workflow_bulk.py`,
   `tests/test_workflow_folders.py`, `tests/test_workflow_tags.py`,
   `tests/test_workflow_delete.py`, `tests/test_workflow_delete_tags.py`.

### Round 5 (2026-09-28): fresh product-loop audit — coverage re-verified, polish landed

A read-only audit re-verified the connector coverage matrix against a live
`registry.catalog()` / `trigger_discovery_catalog()` import: every cell is
backed by a current registry entry, and the designer mirror's action/logic/
filter sets match the registry exactly (one drift caught mid-flight — the
mailchimp trigger chip landed on the Python side before the mirror; chips
always need the `make designer-console` rebuild). All four earlier
"Surprises" were re-checked and confirmed fixed with line evidence: dryrun
derives runners from the registry, a live step lease now requeues the
redelivery instead of skipping it (`LeaseBusy`), the `webhook` action returns
the response body, and poll failures notify the operator.

Polish findings from that audit, landed:

- **OAuth verify-failure refresh fallback** — `tokens.get_access_token` tries
  one `refresh_and_store` when a locally-fresh token fails provider
  verification (server-side revocation/rotation, clock drift), instead of
  wedging every workflow on the connection until natural expiry; the write is
  version-conditional and the replacement token is verified and bind-checked
  before storing. Tests: `tests/test_token_refresh_fallback.py`.
- **429/503 Retry-After under autoretry** — `webhook`/`http_request` failures
  raise a typed `HttpError` carrying `status` (plus a parsed seconds
  `retry_after` where the response headers are readable); an opted-in
  `autoretry` sleeps `min(max_seconds, retry_after)` plus jitter on 429/503
  instead of blind backoff. Steps without `autoretry` are unchanged, and
  provider 4xx/5xx on the default-urllib paths now surface as `HttpError`
  rather than a raw `urllib.error.HTTPError`. Tests: `tests/test_retry_after.py`.
- **`list_versions` scan capped** at `SCAN_LIMIT` like its sibling
  `load_items` (was an unbounded full-table scan on every versions read and
  rollback). Tests: `tests/test_published_workflows.py`.
- **Overview executions block ordered by `started_at`** — the old sort was
  reverse string order on `{workflow}:{action}:{event}` ids, which is not
  chronological, so the block could show stale rows beside the correctly
  ordered runs list. Tests: `tests/test_admin.py`.
- **Error summary/digest honesty about the 200-run cap** — the summary
  payload carries `bounded`/`cap` when the failed-run scan hit the limit and
  the daily digest email renders a "most recent N failed runs are counted"
  caveat, so a >200-failure day no longer reads as exactly 200. Tests:
  `tests/test_error_digest.py`.

Still open (ranked, from the same audit):

1. **CLOSED (2026-09-28)** — Mailchimp webhook lifecycle: a mailchimp hook
   trigger now binds its audience (`list_id`, plus optional subscribed
   `events` and a mailchimp `connection_id`) and drives
   Mailchimp's `POST/DELETE /lists/{id}/webhooks` server-side — save
   subscribes the hook URL (replacing any webhook already registered under
   it) with every source subscribed, and delete/disable unsubscribes by
   listing the audience's webhooks and deleting ours by id. Best-effort on
   both ends: a Mailchimp failure never blocks the stored change, it comes
   back as `warnings` on the response (surfaced by `dapier hooks
   save/delete`); the shared `mailchimp` credential or the bound
   connection's key authenticates, datacenter derived from the key suffix.
   Deliveries match for real: the intake publishes the URL's hook name in
   the event data, a trigger's stored workflow fans out to one trigger spec
   per subscribed type (matching is exact on event, so the old single
   `message.received` spec never fired), and the trigger view/CLI carry no
   bearer hint — Mailchimp sends no auth headers.
   Tests: `tests/test_mailchimp_webhook_lifecycle.py`,
   `tests/test_hook_triggers.py`, `tests/test_mailchimp_intake.py`.
2. **CLOSED (2026-09-28)** — formatter edges: `date_format` takes an
   optional IANA zone as a `@`-suffix on its raw argument
   (`{v|date_format:%Y-%m-%d %H:%M@Europe/Berlin}` — zoneinfo conversion,
   offset-less values read as UTC, unknown zone renders empty per the
   never-raise rule); new zero-arg `time_until` relative formatter
   (`in 3h`, `5m ago`, `just now` within a minute; parses via
   `logic.parse_moment`); `number_format:currency:EUR[:decimals]`
   (€/$/£ table, unknown codes fall back to `1,234.56 JPY` shape),
   validated mode-aware at save time. Tests: `tests/test_templating.py`
   FormatterEdgeTests.
3. **CLOSED (2026-09-28)** — all-workflows export bundle:
   `dapier workflows export --all [-o out.zip]` lists via the agent
   designer-list endpoint, fetches each workflow's canonical YAML through
   the same single-export endpoint, and zips them as flat `<file>.yaml`
   (empty list refuses politely rc 2; a workflow without YAML is skipped
   with rc 5 and the rest bundled). Console parity holds through the
   existing per-workflow Download YAML — both drive the same API. Tests:
   `tests/test_cli.py`.

4. **CLOSED (2026-09-28)** — run-history CSV export, three surfaces over
   one domain function (`runs.api_export`, modeled on the audit trail's
   export): the list's filters (`workflow`, `status` incl. the
   success/problems aliases, `since`/`before`, content search `q`),
   newest first, capped at `max_rows` (default 500, max 2000,
   `truncated` flags the clip), returning
   `{filename, count, truncated, csv}` with one row per run
   (`run_id … error`, booleans as true/false). Audited like every bulk
   read: `runs.export` lands in the operator trail. Surfaces: audited
   `GET /api/admin/runs/export`, operator-gated
   `GET /api/agent/runs/export`, `dapier runs export
   [--workflow|--status|--since|--before|--search|--max-rows|--out]`
   (writes the server's suggested filename by default), and the console
   Runs view's Export CSV button (downloads the filtered history at the
   2000-row cap). Tests: `tests/test_runs.py`, `tests/test_admin.py`,
   `tests/test_agent_api.py`, `tests/test_cli.py`.

5. **CLOSED (2026-09-28)** — YouTube subscribe-on-save: saving, toggling,
   renaming, or rolling back a workflow whose trigger is `youtube /
   video.published` syncs the WebSub hub immediately
   (`youtube_subscriptions.reconcile`, called best-effort from the designer
   store): newly watched channels are subscribed on save, and channels the
   change orphaned are unsubscribed — but only when no other enabled
   workflow still names the channel, and only the delta, so an edit that
   leaves the channel list alone never rings the hub. Disable and delete
   unsubscribe the same way; every hub failure comes back as `warnings` on
   the save/toggle/bulk/delete response (surfaced by `dapier workflows
   save/on/off/delete`) instead of blocking the change, and the 5-day
   renewal schedule remains the reconciliation pass for anything missed.
   Deploy note: the API function now also needs `YOUTUBE_CALLBACK_URL` in
   template.yaml. Tests: `tests/test_youtube_on_save.py`,
   `tests/test_youtube_subscriptions.py`.
6. **CLOSED (2026-09-28)** — Dropbox and Slack action breadth (Zapier's
   file-management and channel-management sets). Dropbox gains
   `dropbox_create_folder` (files/create_folder_v2; an existing folder
   errors — `dropbox_find` with `create_if_missing` stays the find-or-create
   path), `dropbox_move` (files/move_v2 — also Zapier's Rename File: a move
   within the same folder under a new name), and `dropbox_copy`
   (files/copy_v2); both transfer actions render both paths from the event
   and take `autorename` to suffix on conflict, outputting
   `{moved|copied, item}`. Slack gains `slack_dm`
   (conversations.open + chat.postMessage — chain `slack_find_user` and pass
   `{steps.<id>.output.user.id}`), `slack_create_channel`
   (conversations.create; the name is normalized to what Slack accepts —
   lowercase, spaces to hyphens, illegal characters dropped, ≤80 chars — so
   templated names never fail on a stray character), `slack_set_topic` and
   `slack_set_purpose` (conversations.setTopic/setPurpose); alongside the
   update-message and add-reaction actions these complete Zapier's core
   Slack set. All ride the registry (designer mirror + console bundle
   rebuilt) and the existing run/Test-step surfaces — no new endpoints.
   Tests: `tests/test_dropbox_slack_actions.py`.
7. **CLOSED (2026-09-28)** — Slack trigger variety: the Events intake no
   longer folds everything into `message.received`. `slack_events.event_name`
   maps each subscribed Slack type to its own workflow-facing event —
   `message.*` keeps `message.received`, `app_mention` publishes
   `app.mention`, `reaction_added` publishes `reaction.added`,
   `member_joined_channel` publishes `member.joined` (anything else still
   answers `accepted: false`; bot posts still never publish) — and the
   envelope flattens `reaction`, `item_ts` and `inviter` beside the message
   fields. The Slack chip declares all four events, the sample pull serves
   one documented delivery per event (`message.received` stays the live
   channels walk; a reaction or join never shows in history, so the others
   fall back to recorded runs then the documented delivery), and the
   designer mirror's connector catalog carries the same four. Compatibility
   note: `app_mention` no longer also fires `message.received` — workflows
   that relied on that re-filter on `app.mention`
   (docs/connectors/slack.md documents the setup and the mapping).
   Tests: `tests/test_slack_event_variety.py`, `tests/test_slack_events.py`
   (per-event sample pull), `tests/test_slack_actions.py`.
8. **CLOSED (2026-09-28)** — Mailchimp and Google Drive parity round: the
   audience and the drive gain their missing Zapier staples on all
   surfaces. Mailchimp gains `mailchimp_remove_member` (DELETE on the
   email's md5 path; a missing member is `{removed: false}`, not an
   error) and `mailchimp_tag_member` (POST `members/{md5}/tags` — Add
   applies the tag, Remove sets it inactive), and its first poll source:
   `mailchimp.members` lists one audience through the Marketing API with
   the stored key (a named `connection_id` or the shared `mailchimp`
   credential — no OAuth required) and publishes `mailchimp`/`member.new`
   on a `last_changed` watermark — seeded first fire (the roster is
   history, not news), oldest-first emission, and the seen-set keeping a
   profile edit from re-firing an already-seen member. Google Drive gains
   the change-feed sources Zapier's Updated/Deleted File triggers need —
   `google-drive.updates` (`file.updated`, folder-scoped when `folder_id`
   is stored) and `google-drive.deletions` (`file.deleted`, always
   Drive-wide: a removal change carries a fileId only) — sharing one
   `changes.list` fetch whose opaque page-token cursor parks only once a
   page drains, so the budget and seen-set machinery work unchanged; plus
   `drive_share_file` (permissions.create — user/group/domain/email
   grantee) and `drive_copy_file` (files.copy). The chips declare
   `member.new` / `file.updated` / `file.deleted`, the sample pulls serve
   each (stored-poll live pulls first: Drive's changes sources sample
   against the parked cursor read-only, Mailchimp's against the epoch
   watermark; fallbacks key on the poll's event), and the designer mirror
   carries the actions and events (console bundle rebuilt). Tests:
   `tests/test_round_mailchimp_drive.py`, `tests/test_mailchimp_actions.py`,
   `tests/test_mailchimp_poll.py`, `tests/test_drive_changes.py`.
9. **CLOSED (2026-09-28)** — Slack gets its no-app poll source:
   `slack.messages` lists one channel through `conversations.history` (two
   pages of 200, watermark = the newest ts) with the stored bot token and
   publishes the same `message.received` event the Events intake does —
   scheduled polling feeds the identical pipeline with no Slack app and no
   Event Subscriptions setup; the first fire seeds without emitting, and
   bot posts and membership subtypes never flatten (the intake's rule).
   The chip needs nothing new; the sample pull walks the channel live.
   Tests: `tests/test_round_slack.py`.
10. **CLOSED (2026-09-28)** — Sheets and Telegram action staples:
   `sheets_delete_row` (worksheet title → sheetId, then batchUpdate
   deleteDimension; pairs with sheets_lookup_row's row output) and
   `sheets_create_spreadsheet` (spreadsheets.create, optional header row
   written through the existing values.update path — the output
   spreadsheet_id/url feed a follow-up append); `telegram_send_photo` and
   `telegram_send_document` (Bot API multipart, chat_id falling back to
   the triggering chat; media comes from source_url — dapier fetches it
   itself, so Telegram need not reach it — or a staged source_s3
   {bucket, key}). Tests: `tests/test_round_sheetstelegram.py`.
11. **CLOSED (2026-09-28)** — YouTube, S3 and Zoom action staples plus
   http_request response visibility: `youtube_add_to_playlist`
   (playlistItems.insert — directly serves the DTC channel workflow) and
   `youtube_update_video` (videos.update part=snippet; title required —
   YouTube replaces the snippet whole, and omitting category clears it);
   `s3_read_object` (GetObject staged like the dropbox read),
   `s3_presign_url` (presigned GET, one hour by default up to SigV4's
   seven-day ceiling — the dropbox get_temp_link symmetry) and
   `s3_delete_object` (idempotent DeleteObject); `zoom_update_meeting`
   (PATCH /meetings/{id} with only the filled fields — an empty update is
   rejected) and `zoom_add_registrant` (POST registrants; join_url is
   unique per registrant). `http_request` and the webhook action surface
   `response_headers` (lowercased) whenever the transport surfaced them —
   legacy two-key outputs unchanged. Tests: `tests/test_round_yts3zoom.py`,
   `tests/test_http_response_headers.py`.
12. **CLOSED (2026-09-28)** — mailchimp.members watermark hardened: the
   cursor is now the composite `last_changed|id` (the zoom and s3 sources'
   pattern), so members changed in the same second as the last fired
   member are no longer skipped until something else bumps the watermark.
   Tests: `tests/test_round_mailchimp_drive.py` (same-second regression
   pin).
13. **CLOSED (2026-09-28)** — Multi-user roles v1: authorization grows from
   a single operator allowlist plus per-connection grants to a named user
   role model. A DynamoDB store (`RoleAssignmentsTable`, one row per DTC
   subject or email, in `src/dapier/auth/roles.py`) carries a role —
   `viewer` (read-only console: overview, runs, audit, designer reads,
   discovery, trigger samples), `editor` (also workflow editing/testing,
   replays and cancels, storage writes), `operator` (the historical
   capability set: also connections, credentials, grants, tokens, trigger
   definitions), `admin` (also user management) — plus an optional display
   name and a `disabled` flag that denies an account everywhere. Resolution:
   a stored row wins, narrowing an allowlisted operator to viewer or
   widening a non-allowlisted identity into a band; with no stored row the
   `OPERATOR_EMAILS`/`OPERATOR_SUBJECTS` allowlist stays authoritative —
   answering `admin` while the store is empty, so today's operators keep
   full power and can bootstrap the first admin, and `operator` once any row
   exists — which makes an empty table reproduce the pre-roles gate bit for
   bit, and store failures degrade to the allowlist verdict rather than
   failing a request. The last active admin cannot be demoted, disabled, or
   removed (the API refuses with 409). Enforcement is centralized in the
   auth layer: the console dispatcher gates every route through
   `session.require_role(event, roles.minimum_for_route(method, path))`, the
   agent API through `require_operator`'s `minimum_for_action` band, both
   sharing `roles.satisfies(roles.effective_role(payload))`. Surfaces
   (parity): console `GET/POST /api/admin/users` plus
   `DELETE /api/admin/users/{subject}` behind a Users view (list, set-role
   dialog with display name and disabled flag, remove with confirm);
   `dapier users list|set-role|remove` over the operator- and admin-gated
   `/api/agent/users`; every mutation audited as `users.set-role` /
   `users.remove`. Tests: `tests/test_roles.py` (domain semantics, dispatcher
   bands, bootstrap-then-narrow transitions, last-admin guard, disabled and
   API-token denials, agent users API), `tests/test_users_cli.py` (command
   surface, confirmations, exit codes). Shared workspaces and invitations
   remain future work.
14. **CLOSED (2026-09-28)** — find-or-create breadth and the mailchimp
   unsubscribe staple. `mailchimp_unsubscribe_member` PATCHes the member's
   status to unsubscribed — the reversible counterpart of the permanent
   remove (upsert's `status_if_new` only applies to members the audience
   does not have yet, so it could not unsubscribe an existing one); a
   missing member is `{unsubscribed: false}`, not an error.
   `create_if_missing` now covers every find with a meaningful create:
   `mailchimp_find_member` upserts on a miss (`status` applies as
   status-if-new, optional merge fields, `created: true` in the output —
   Zapier's Find or Create Member), and `slack_find`'s channel branch
   creates a missed name via `conversations.create` (`is_private` honored;
   Zapier's Find or Create Channel — a user miss stays a miss), joining
   the existing sheets/dropbox/zoom finds. Engine runners, registry
   entries and designer catalog mirrors; tests:
   `tests/test_mailchimp_actions.py`, `tests/test_find_slack_dropbox.py`.

14. **CLOSED (2026-09-28)** — Workflow templates v1: a workflow flagged
   `template: true` (the flag rides the workflow YAML, set through
   `api_template_flag`) joins the gallery that `api_templates` serves —
   the bundled starters in `workflows/template-*.yaml` plus anything an
   operator published, the published overlay winning per id. Forking is
   `api_apply_template`: the flagged workflow loads under a new
   `<template-id>-copy` id and goes through the same commit-and-publish
   path as a save. All three surfaces: the designer's "Start from a
   template" gallery (console bundle rebuilt), `GET/POST
   /api/admin/designer/templates*`, the operator-gated `/api/agent/designer/templates*`
   trio, and `dapier templates list|apply|publish|unpublish`. Tests:
   `tests/test_designer.py` (store + admin routes), `tests/test_cli.py`
   (command surface). Cross-account transfer still needs the multi-user
   model (item 13); marketplace-style sharing beyond this deployment
   remains future work.
15. **CLOSED (2026-09-28)** — Designer editor polish: undo/redo over a
   per-session draft timeline (`designer/src/history.ts`, pure module —
   500ms sliding coalescing per editing group, 50-entry cap, reset on
   open and after a successful save; Ctrl/Cmd+Z, Ctrl/Cmd+Shift+Z /
   Ctrl+Y, toolbar buttons), step duplication (fresh id, `-copy`/`(copy)`
   suffixes, inserted after the original), cross-workflow step copy/paste
   (localStorage clipboard, pasted steps validate like manual ones), and
   a "?" shortcuts overlay. Editor-session state only — no API surface,
   so under the parity rule's presentational exception.
16. **CLOSED (2026-09-28)** — Zoom round completion, behind item 11:
   `zoom_delete_meeting` (DELETE /meetings/{id}, optional occurrence_id
   for recurring scoping, output `{deleted, meeting_id}`) and
   find-or-create on `zoom_find_meeting` (`create_if_missing` reuses the
   create path's payload rules; a hit or a meeting-id lookup never
   creates; output gains `created`). Find-or-create verdicts elsewhere:
   `slack_find_user` (no user-create API), `drive_find_file` (a miss
   composes with drive_upload_file), `s3_find` (an empty object is
   storage noise) — deliberately skipped, documented in
   `docs/connector-coverage-audit.md`. Tests: `tests/test_zoom_delete_meeting.py`,
   `tests/test_slack_drive_media.py`, `tests/test_find_media.py`.

Reviewed 2026-09-27 against committed HEAD (`97b2a2c`) plus the in-flight working
tree (trigger discovery and designer changes are being built separately; noted
where they land). Read alongside `docs/architecture-review.md`, which covers the
registry/execution refactor; this document covers the *product loop* gaps beyond
the discovery/replay/test work already underway.

## 1. The Zapier loop, mapped to dapier today

| Zapier concept | Dapier equivalent | Status |
| --- | --- | --- |
| Trigger connectors (webhook, email, schedule, polling) | hook/email/schedule/poll triggers (`src/dapier/triggers/`), registry chips (`connectors/registry.py`) | have |
| Trigger sample discovery ("find recent data") | sample for every chip (`connectors/trigger_discovery.py`); options discovery for every registry listing, invariant-tested (`tests/test_options_breadth.py`); dispatch shared by `/api/{agent,admin}/discover` | have |
| Action catalog with field schemas | `registry.Action.fields`, served by `GET /api/catalog` (`connectors/registry.py:146`) | have |
| Search / find-record actions (lookup, create-if-missing) | 14 find actions with `create_if_missing` (e.g. `sheets_find_row`, `dropbox_find`, `slack_find`, `zoom_find_meeting`, `mailchimp_find_member`) | have |
| Multi-step workflows with data mapping | `steps` context + formatters (`engine/actions/templating.py:38`), `http_request` output capture (`connectors/webhook.py:44`) | have |
| Filters and branching (Paths) | `filter`/`condition`/n-way `paths` on the full trigger-filter operator set; rules read earlier `steps` outputs (`engine/logic.py`, `matching._matches_filter`) | have |
| Looping | `for_each` (`engine/logic.py:275`) | have |
| Delay / "schedule after" / requeue-until | `RunSuspended` requeue-until (`engine/logic.py` `_run_delay`, `engine/worker.py` `_resume_run`) | have |
| Digests / batching | `digest_add`/`digest_flush` + `digest` logic step (`connectors/digest.py`, `engine/actions/digests.py`) | have |
| Per-step error handling / on-failure action | `on_error`/`on_fail` (`engine/logic.py` `_handle_error`, `connectors/registry.py` validation) | have |
| Retry/backoff policy per action | workflow-level `retry` redrive + per-step `autoretry` with exponential backoff (`engine/worker.py`, `engine/logic.py`) | have |
| Task history (runs, step I/O) | EXECUTIONS_TABLE + run grouping (`api/runs.py`) | have |
| Replay a task | `api_replay` re-injects the envelope onto the queue (`api/runs.py:190`) | have |
| Test before publish (dry-run/execute) | `engine/dryrun.py`, designer Test panel, `wf test` CLI | have (stale runner list, below) |
| Field mapping autofill from sample data | trigger-sample endpoint feeds the inspector and Test step (`api/runs.py` `api_trigger_sample`, `engine/dryrun.py` `test_step`) | have |
| Workflow versioning / rollback | `#v<n>` version records in PUBLISHED_WORKFLOWS_TABLE (`triggers/published_workflows.py`); versions list + rollback on console, CLI, and API | have |
| Find my zap (cross-workflow search) | `?q=` search over workflows and run content (`api/overview.py` `_workflow_matches`, `api/runs.py` `api_list`) | have |
| Task usage metering | task counting + monthly rollup (`engine/usage.py`, `dapier usage`); no plan caps yet | have (metering; caps open) |
| Error alerting to the user | opt-in email per failed run + error digest (`engine/notify.py`, `error_digest.py`) | have |
| Save-time input validation | required/unknown keys, template syntax, field types (`connectors/registry.py` `validate_action_chain`, `validate_field_types`) | have |
| Sub-zaps (call another workflow) | `run_workflow` step (`connectors/subworkflow.py`) | have |
| Data stores (Zapier Storage API) | `storage_get/set/delete/find` + storage API + console view (`connectors/storage.py`, `api/storage.py`, `views/storage.js`) | have |
| Outbound webhooks with signing/retry | HMAC signing + typed `HttpError` + per-step `autoretry` (`engine/actions/webhook.py`, `engine/logic.py`) | have |
| Templates / shared zaps (marketplace) | `template: true` flag rides the workflow YAML; gallery + apply (fork) + publish/unpublish on console (designer), CLI (`dapier templates`), and API (`/designer/templates*`); bundled starters in `workflows/template-*.yaml` | have (v1: single-tenant gallery; cross-account transfer still needs multi-user) |

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

### Connector coverage matrix (2026-09-28 audit)

Every capability cell is backed by a registry entry, verified by importing
`connectors` and dumping `registry.catalog()` +
`trigger_discovery.trigger_discovery_catalog()`. "—" means genuinely
not-applicable (no connection record, no remote listing), not missing.

Trigger connectors (palette chips + sample pull):

| connector | events | sample pull | trigger options | action pickers | conn test | find actions |
|---|---|---|---|---|---|---|
| email | message.received | ✓ | — | — | — (no connection record) | — (send only) |
| youtube | video.published | ✓ | — (push, no trigger fields) | playlists | youtube | find video, find playlist videos, upload video (needs `youtube.upload` scope — see docs/connectors/google.md) |
| dropbox | file.created/updated/deleted | ✓ per event | folders | files, folders, search | dropbox | find file or folder |
| zoom | recording.completed, recording.transcript_completed, meeting.started, meeting.ended | ✓ per event | meetings | meetings, recordings | zoom | find meeting, find recording |
| slack | message.received, app.mention, reaction.added, member.joined | ✓ per event | channels | channels, users, messages | slack | find user or channel (create-if-missing on channels), find user by email, find message, update message, add reaction, pin, invite users, DM, create channel, set topic/purpose, upload file, schedule, add reminder |
| telegram | message.received, channel_post.received | ✓ per event | chats | chats | telegram | send, send photo, send document, send poll, find chat |
| renderer | job.completed | ✓ | — | — | — (internal) | — |
| schedule | schedule.triggered | ✓ | — | — | — | — |
| poll | item.new | ✓ | — | — | — | — |
| custom (webhook ingress) | freeform | ✓ | — | — | — | — |

Action-only providers (credential-backed, no trigger chip):

| provider | action pickers | conn test | find actions |
|---|---|---|---|
| google (Sheets + Drive) | spreadsheets, worksheets, columns, rows, files, folders | google | find/lookup/update row, find file |
| s3 / aws keys | buckets, objects | aws | find object |
| mailchimp | audiences, members | mailchimp | find member, upsert member |

Everything else (code/js, logic steps, storage, digest, delay, webhook and
http actions, run_workflow) is internal or generic and needs no remote
listing; `dataops` is action-side intake, so its sample pull is for test
panels only and it deliberately has no palette chip.

One hole the audit closed: **telegram had no palette chip** — its hook
trigger, sample pull, chat options, connection test, and find-chat action
all existed, but `CONNECTORS` never listed it, so the designer's trigger
palette and `GET /api/catalog` had no Telegram entry. Added to
`connectors/triggers.py` (`message.received`, like the hook engine's
`TELEGRAM_EVENT`); `tests/test_trigger_samples.py` now asserts every
sample-discoverable connector has a chip. The webhook-ingress sample stays
keyed `webhook` — it surfaces as the Custom chip.

### Open findings from the 2026-09-28 product-loop audit (ranked)

1. **CLOSED (2026-09-28)** — replay-from-step: `runs.api_replay(run_id,
   from_step=<top-level step id>)` reuses the worker's own park-and-continue
   path — a synthetic `dapier_resume` envelope carries the workflow's tail
   from the chosen step plus the recorded outputs of everything before it
   (seeded `step_outputs`, so templates still read `{steps.<id>.output.*}`),
   and a fresh event id lands the rerun in history tied to the original by
   `correlation_id`. Full replay (re-injected trigger) stays the default;
   the early steps never re-fire on a targeted retry. On every surface:
   `POST /api/{admin,agent}/runs/{id}/replay` with a `from_step` body
   (audited as `runs.replay-from-step`), `dapier runs replay --from-step`,
   and a per-step "Replay from here" button in the console run dialog.
   Refusals are explicit: workflow gone (404), disabled (409), a step name
   nothing records or defines (404), or a recorded step that is not a
   top-level step of the current definition (409). Tests:
   `tests/test_runs.py`, `tests/test_agent_api.py`, `tests/test_admin.py`,
   `tests/test_cli.py`.
2. **CLOSED (2026-09-28)** — run-history content search: `?q=` on
   `GET /api/{admin,agent}/runs` does a case-insensitive substring match
   over each run's recorded step data (input, output, error) and ids inside
   the same bounded scan window the list already walks, so "which run
   carried order #1234" answers from history; composes with the
   workflow/status/time filters and keyset paging, and `paging.filtered`
   stays honest. Wired to `dapier runs list --search` and the console
   Runs view's debounced search box. Tests: `tests/test_runs.py`,
   `tests/test_agent_api.py`, `tests/test_cli.py`.
3. **CLOSED (2026-09-28)** — listings carry an offline `health:
   ok|expired` + `token_expires_at` computed from the stored OAuth token
   (`records.public_view(item, stored)`, fed by `tokens.stored_value` on
   `/api/agent/connections` and `/api/admin/overview`). The console
   Connections view turns expired tokens into a live "needs reconnection"
   row with a Reconnect action — counts, filter, and sort follow — and
   `dapier connections list|show` print HEALTH/EXPIRES. Tests:
   `tests/test_agent_api.py`, `tests/test_admin.py`, `tests/test_cli.py`.
4. **CLOSED (2026-09-28)** — the daily operator error digest is scheduled
   (`ErrorDigestFunction`, cron 07:00 UTC): `error_digest.py` renders the
   `api_summary` rollup into one SES email to the operator address, a
   zero-failure window skips the send, and function errors page the alarm
   chain (`ErrorDigestErrorAlarm`). Send-now parity: operator-gated
   `POST /api/{admin,agent}/errors/digest` (audited `errors.send-digest`
   on real sends), `dapier errors send-digest`, and the console
   Failed-runs panel's "Send digest now". Tests: `tests/test_error_digest.py`.
5. **CLOSED (2026-09-28)** — workflow delete on every surface:
   `designer_store.api_delete` unpublishes the live item (the engine stops
   matching on the next event), then removes `workflows/<file>` in one
   atomic git tree-delete commit (`commit_delete`, a `sha: null` entry
   against the base tree — the same mechanism a rename uses), refusing 409
   while any run is parked on a delay (a resume would dangle into a
   definition that no longer exists; the operator cancels or waits them
   out). Run history is untouched — deleting stops the automation, not the
   audit trail. A git failure after a live delete reports
   `git_sync_error` and still counts as deleted. Surfaces: audited
   `DELETE /api/{admin,agent}/designer/workflows/<file>` (console Delete
   button with confirm), `dapier workflows delete [--yes]` (warns when the
   commit failed). Tests: `tests/test_workflow_delete_tags.py`.
6. **CLOSED (2026-09-28)** — webhook triggers answer sync: `response: {mode:
   ack|challenge|sync, template}` on the stored hook (webhook kind only —
   Telegram keeps its fast fixed ack), validated at save on both dispatchers
   through `build_item`. `ack` (the default) is the old fixed 202;
   `challenge` answers verification handshakes (query `challenge` /
   `hub.challenge`, falling back to the body's `challenge`) with plain-text
   200 and runs nothing; `sync` runs the matched workflow inline in the
   ingress — the production path (step leases, run history, task usage,
   failure notify, workflow retry) via `worker.execute` with the worker's own
   attempt hooks — and answers 200 with the `template` rendered against
   `{trigger.*}` / `{steps.<id>.output.*}` (any JSON shape; default body
   `{"ok", "workflow", "steps", "event_id"}`), 202 `{"suspended": true,
   "resume_at": …}` when a delay parks the run (the continuation is enqueued
   exactly as the worker parks it), or 500 with the delivery's dedupe claim
   released (a provider retry re-executes) when the chain fails. Optional
   `status` (2xx, default 200) sets the success code and `budget_seconds`
   (1-25, default 10) the inline run's wall-clock budget. Dedupe composes: a
   retried delivery is answered, not re-run. Sync is a single-workflow
   contract — zero or several matching workflows fall back to the ack path
   (202, with a `reason`) — and the budget is enforced between steps, never
   mid-step: on overrun the un-run tail is enqueued under the same event id
   (the completed steps' leases dedupe the worker's replay) and the caller
   gets 202 `{"reason": "budget"}`, so the run still finishes out of band;
   longer chains belong in ack mode. Surfaces: the console hook dialog's
   Response select + template JSON field, `dapier hooks save` body files
   plus `--sync-response` (shown by `hooks show`). Tests:
   `tests/test_hook_sync_response.py`, `tests/test_sync_response.py`,
   `tests/test_webhook_sync.py`.
7. **CLOSED (2026-09-28)** — trigger dedupe: a TTL-bounded seen-id store
   (`triggers/seen.py`, one item per scope in the cursors table, 30-day
   expiry, 1000-entry cap, atomic `claim`/`forget`) closes both halves.
   `next_cursor` polls now skip items whose stable id was already published
   (providers recycle pages) and park the provider's continuation cursor —
   never an item id — once a page drained; watermark polls are unchanged
   (the cursor already dedupes). Webhook triggers take an optional
   `dedupe_path` (dot-path to a stable delivery id in the payload; console
   Triggers form and CLI `hooks save` both set it): a retried delivery
   claims the same event id, answers `202 duplicate`, and never publishes —
   a publish failure releases the claim so the retry still delivers, and a
   seen-store outage publishes anyway (the stable id still dedupes
   downstream through run grouping and step leases). Telegram triggers
   dedupe by default on `update_id`, the provider's own delivery identity.
   Tests: `tests/test_seen_store.py`, `tests/test_poll_dedupe.py`,
   `tests/test_poll_triggers.py`, `tests/test_hook_dedupe.py`.
8. **CLOSED (2026-09-28)** — tags: a workflow carries up to 20
   short labels in its YAML (`tags:`, validated by `_validate_tags`), set
   through `PUT /api/{admin,agent}/designer/workflows/<file>/tags`
   (audited `workflow.tags`, published with cause "tags", committed
   best-effort like the toggle), `dapier workflows tags <file> --tags
   a,b | --clear`, and a console Tags prompt per row. Tags are normalized
   to lowercase, deduped case-insensitively, and bounded (20 × 64 chars).
   Both list endpoints narrow with `?tag=` (case-insensitive) — the CLI
   mirrors it with `workflows list --tag`, and the console's Workflows
   view gets a tag dropdown plus tag chips per row. Tags travel with
   saves, deploys, duplicates, and rollbacks because they live in the
   definition. Bulk enable/disable is also in: audited
   `POST /api/{admin,agent}/designer/workflows/bulk`
   (`{"ids": [...], "action": "enable|disable"}` with per-id results,
   refusing parked-delay workflows like the single toggle) and
   `dapier workflows on|off <file>...` taking multiple files in one call.
   And folders close the slice: a flat `folder:` top-level YAML key
   (`_validate_folder` — ≤64 chars, never a path; Zapier folders are
   flat), `PUT /api/{admin,agent}/designer/workflows/<file>/folder`
   (audited `workflow.folder`, cause "folder"), `dapier workflows folder
   <file> --set "Name" | --clear` and `workflows list --folder`, `?folder=`
   on both list routes plus the overview payload (a `workflow_folders`
   aggregate beside the tags one), and a console Folder dropdown + folder
   chips + per-row Folder prompt. Tests: `tests/test_workflow_bulk.py`,
   `tests/test_workflow_folders.py`, `tests/test_workflow_tags.py`,
   `tests/test_workflow_delete.py`, `tests/test_workflow_delete_tags.py`.

### Round 5 (2026-09-28): fresh product-loop audit — coverage re-verified, polish landed

A read-only audit re-verified the connector coverage matrix against a live
`registry.catalog()` / `trigger_discovery_catalog()` import: every cell is
backed by a current registry entry, and the designer mirror's action/logic/
filter sets match the registry exactly (one drift caught mid-flight — the
mailchimp trigger chip landed on the Python side before the mirror; chips
always need the `make designer-console` rebuild). All four earlier
"Surprises" were re-checked and confirmed fixed with line evidence: dryrun
derives runners from the registry, a live step lease now requeues the
redelivery instead of skipping it (`LeaseBusy`), the `webhook` action returns
the response body, and poll failures notify the operator.

Polish findings from that audit, landed:

- **OAuth verify-failure refresh fallback** — `tokens.get_access_token` tries
  one `refresh_and_store` when a locally-fresh token fails provider
  verification (server-side revocation/rotation, clock drift), instead of
  wedging every workflow on the connection until natural expiry; the write is
  version-conditional and the replacement token is verified and bind-checked
  before storing. Tests: `tests/test_token_refresh_fallback.py`.
- **429/503 Retry-After under autoretry** — `webhook`/`http_request` failures
  raise a typed `HttpError` carrying `status` (plus a parsed seconds
  `retry_after` where the response headers are readable); an opted-in
  `autoretry` sleeps `min(max_seconds, retry_after)` plus jitter on 429/503
  instead of blind backoff. Steps without `autoretry` are unchanged, and
  provider 4xx/5xx on the default-urllib paths now surface as `HttpError`
  rather than a raw `urllib.error.HTTPError`. Tests: `tests/test_retry_after.py`.
- **`list_versions` scan capped** at `SCAN_LIMIT` like its sibling
  `load_items` (was an unbounded full-table scan on every versions read and
  rollback). Tests: `tests/test_published_workflows.py`.
- **Overview executions block ordered by `started_at`** — the old sort was
  reverse string order on `{workflow}:{action}:{event}` ids, which is not
  chronological, so the block could show stale rows beside the correctly
  ordered runs list. Tests: `tests/test_admin.py`.
- **Error summary/digest honesty about the 200-run cap** — the summary
  payload carries `bounded`/`cap` when the failed-run scan hit the limit and
  the daily digest email renders a "most recent N failed runs are counted"
  caveat, so a >200-failure day no longer reads as exactly 200. Tests:
  `tests/test_error_digest.py`.

Still open (ranked, from the same audit):

1. **CLOSED (2026-09-28)** — Mailchimp webhook lifecycle: a mailchimp hook
   trigger now binds its audience (`list_id`, plus optional subscribed
   `events` and a mailchimp `connection_id`) and drives
   Mailchimp's `POST/DELETE /lists/{id}/webhooks` server-side — save
   subscribes the hook URL (replacing any webhook already registered under
   it) with every source subscribed, and delete/disable unsubscribes by
   listing the audience's webhooks and deleting ours by id. Best-effort on
   both ends: a Mailchimp failure never blocks the stored change, it comes
   back as `warnings` on the response (surfaced by `dapier hooks
   save/delete`); the shared `mailchimp` credential or the bound
   connection's key authenticates, datacenter derived from the key suffix.
   Deliveries match for real: the intake publishes the URL's hook name in
   the event data, a trigger's stored workflow fans out to one trigger spec
   per subscribed type (matching is exact on event, so the old single
   `message.received` spec never fired), and the trigger view/CLI carry no
   bearer hint — Mailchimp sends no auth headers.
   Tests: `tests/test_mailchimp_webhook_lifecycle.py`,
   `tests/test_hook_triggers.py`, `tests/test_mailchimp_intake.py`.
2. **CLOSED (2026-09-28)** — formatter edges: `date_format` takes an
   optional IANA zone as a `@`-suffix on its raw argument
   (`{v|date_format:%Y-%m-%d %H:%M@Europe/Berlin}` — zoneinfo conversion,
   offset-less values read as UTC, unknown zone renders empty per the
   never-raise rule); new zero-arg `time_until` relative formatter
   (`in 3h`, `5m ago`, `just now` within a minute; parses via
   `logic.parse_moment`); `number_format:currency:EUR[:decimals]`
   (€/$/£ table, unknown codes fall back to `1,234.56 JPY` shape),
   validated mode-aware at save time. Tests: `tests/test_templating.py`
   FormatterEdgeTests.
3. **CLOSED (2026-09-28)** — all-workflows export bundle:
   `dapier workflows export --all [-o out.zip]` lists via the agent
   designer-list endpoint, fetches each workflow's canonical YAML through
   the same single-export endpoint, and zips them as flat `<file>.yaml`
   (empty list refuses politely rc 2; a workflow without YAML is skipped
   with rc 5 and the rest bundled). Console parity holds through the
   existing per-workflow Download YAML — both drive the same API. Tests:
   `tests/test_cli.py`.

4. **CLOSED (2026-09-28)** — run-history CSV export, three surfaces over
   one domain function (`runs.api_export`, modeled on the audit trail's
   export): the list's filters (`workflow`, `status` incl. the
   success/problems aliases, `since`/`before`, content search `q`),
   newest first, capped at `max_rows` (default 500, max 2000,
   `truncated` flags the clip), returning
   `{filename, count, truncated, csv}` with one row per run
   (`run_id … error`, booleans as true/false). Audited like every bulk
   read: `runs.export` lands in the operator trail. Surfaces: audited
   `GET /api/admin/runs/export`, operator-gated
   `GET /api/agent/runs/export`, `dapier runs export
   [--workflow|--status|--since|--before|--search|--max-rows|--out]`
   (writes the server's suggested filename by default), and the console
   Runs view's Export CSV button (downloads the filtered history at the
   2000-row cap). Tests: `tests/test_runs.py`, `tests/test_admin.py`,
   `tests/test_agent_api.py`, `tests/test_cli.py`.

5. **CLOSED (2026-09-28)** — YouTube subscribe-on-save: saving, toggling,
   renaming, or rolling back a workflow whose trigger is `youtube /
   video.published` syncs the WebSub hub immediately
   (`youtube_subscriptions.reconcile`, called best-effort from the designer
   store): newly watched channels are subscribed on save, and channels the
   change orphaned are unsubscribed — but only when no other enabled
   workflow still names the channel, and only the delta, so an edit that
   leaves the channel list alone never rings the hub. Disable and delete
   unsubscribe the same way; every hub failure comes back as `warnings` on
   the save/toggle/bulk/delete response (surfaced by `dapier workflows
   save/on/off/delete`) instead of blocking the change, and the 5-day
   renewal schedule remains the reconciliation pass for anything missed.
   Deploy note: the API function now also needs `YOUTUBE_CALLBACK_URL` in
   template.yaml. Tests: `tests/test_youtube_on_save.py`,
   `tests/test_youtube_subscriptions.py`.
6. **CLOSED (2026-09-28)** — Dropbox and Slack action breadth (Zapier's
   file-management and channel-management sets). Dropbox gains
   `dropbox_create_folder` (files/create_folder_v2; an existing folder
   errors — `dropbox_find` with `create_if_missing` stays the find-or-create
   path), `dropbox_move` (files/move_v2 — also Zapier's Rename File: a move
   within the same folder under a new name), and `dropbox_copy`
   (files/copy_v2); both transfer actions render both paths from the event
   and take `autorename` to suffix on conflict, outputting
   `{moved|copied, item}`. Slack gains `slack_dm`
   (conversations.open + chat.postMessage — chain `slack_find_user` and pass
   `{steps.<id>.output.user.id}`), `slack_create_channel`
   (conversations.create; the name is normalized to what Slack accepts —
   lowercase, spaces to hyphens, illegal characters dropped, ≤80 chars — so
   templated names never fail on a stray character), `slack_set_topic` and
   `slack_set_purpose` (conversations.setTopic/setPurpose); alongside the
   update-message and add-reaction actions these complete Zapier's core
   Slack set. All ride the registry (designer mirror + console bundle
   rebuilt) and the existing run/Test-step surfaces — no new endpoints.
   Tests: `tests/test_dropbox_slack_actions.py`.
7. **CLOSED (2026-09-28)** — Slack trigger variety: the Events intake no
   longer folds everything into `message.received`. `slack_events.event_name`
   maps each subscribed Slack type to its own workflow-facing event —
   `message.*` keeps `message.received`, `app_mention` publishes
   `app.mention`, `reaction_added` publishes `reaction.added`,
   `member_joined_channel` publishes `member.joined` (anything else still
   answers `accepted: false`; bot posts still never publish) — and the
   envelope flattens `reaction`, `item_ts` and `inviter` beside the message
   fields. The Slack chip declares all four events, the sample pull serves
   one documented delivery per event (`message.received` stays the live
   channels walk; a reaction or join never shows in history, so the others
   fall back to recorded runs then the documented delivery), and the
   designer mirror's connector catalog carries the same four. Compatibility
   note: `app_mention` no longer also fires `message.received` — workflows
   that relied on that re-filter on `app.mention`
   (docs/connectors/slack.md documents the setup and the mapping).
   Tests: `tests/test_slack_event_variety.py`, `tests/test_slack_events.py`
   (per-event sample pull), `tests/test_slack_actions.py`.
8. **CLOSED (2026-09-28)** — Mailchimp and Google Drive parity round: the
   audience and the drive gain their missing Zapier staples on all
   surfaces. Mailchimp gains `mailchimp_remove_member` (DELETE on the
   email's md5 path; a missing member is `{removed: false}`, not an
   error) and `mailchimp_tag_member` (POST `members/{md5}/tags` — Add
   applies the tag, Remove sets it inactive), and its first poll source:
   `mailchimp.members` lists one audience through the Marketing API with
   the stored key (a named `connection_id` or the shared `mailchimp`
   credential — no OAuth required) and publishes `mailchimp`/`member.new`
   on a `last_changed` watermark — seeded first fire (the roster is
   history, not news), oldest-first emission, and the seen-set keeping a
   profile edit from re-firing an already-seen member. Google Drive gains
   the change-feed sources Zapier's Updated/Deleted File triggers need —
   `google-drive.updates` (`file.updated`, folder-scoped when `folder_id`
   is stored) and `google-drive.deletions` (`file.deleted`, always
   Drive-wide: a removal change carries a fileId only) — sharing one
   `changes.list` fetch whose opaque page-token cursor parks only once a
   page drains, so the budget and seen-set machinery work unchanged; plus
   `drive_share_file` (permissions.create — user/group/domain/email
   grantee) and `drive_copy_file` (files.copy). The chips declare
   `member.new` / `file.updated` / `file.deleted`, the sample pulls serve
   each (stored-poll live pulls first: Drive's changes sources sample
   against the parked cursor read-only, Mailchimp's against the epoch
   watermark; fallbacks key on the poll's event), and the designer mirror
   carries the actions and events (console bundle rebuilt). Tests:
   `tests/test_round_mailchimp_drive.py`, `tests/test_mailchimp_actions.py`,
   `tests/test_mailchimp_poll.py`, `tests/test_drive_changes.py`.
9. **CLOSED (2026-09-28)** — Slack gets its no-app poll source:
   `slack.messages` lists one channel through `conversations.history` (two
   pages of 200, watermark = the newest ts) with the stored bot token and
   publishes the same `message.received` event the Events intake does —
   scheduled polling feeds the identical pipeline with no Slack app and no
   Event Subscriptions setup; the first fire seeds without emitting, and
   bot posts and membership subtypes never flatten (the intake's rule).
   The chip needs nothing new; the sample pull walks the channel live.
   Tests: `tests/test_round_slack.py`.
10. **CLOSED (2026-09-28)** — Sheets and Telegram action staples:
   `sheets_delete_row` (worksheet title → sheetId, then batchUpdate
   deleteDimension; pairs with sheets_lookup_row's row output) and
   `sheets_create_spreadsheet` (spreadsheets.create, optional header row
   written through the existing values.update path — the output
   spreadsheet_id/url feed a follow-up append); `telegram_send_photo` and
   `telegram_send_document` (Bot API multipart, chat_id falling back to
   the triggering chat; media comes from source_url — dapier fetches it
   itself, so Telegram need not reach it — or a staged source_s3
   {bucket, key}). Tests: `tests/test_round_sheetstelegram.py`.
11. **CLOSED (2026-09-28)** — YouTube, S3 and Zoom action staples plus
   http_request response visibility: `youtube_add_to_playlist`
   (playlistItems.insert — directly serves the DTC channel workflow) and
   `youtube_update_video` (videos.update part=snippet; title required —
   YouTube replaces the snippet whole, and omitting category clears it);
   `s3_read_object` (GetObject staged like the dropbox read),
   `s3_presign_url` (presigned GET, one hour by default up to SigV4's
   seven-day ceiling — the dropbox get_temp_link symmetry) and
   `s3_delete_object` (idempotent DeleteObject); `zoom_update_meeting`
   (PATCH /meetings/{id} with only the filled fields — an empty update is
   rejected) and `zoom_add_registrant` (POST registrants; join_url is
   unique per registrant). `http_request` and the webhook action surface
   `response_headers` (lowercased) whenever the transport surfaced them —
   legacy two-key outputs unchanged. Tests: `tests/test_round_yts3zoom.py`,
   `tests/test_http_response_headers.py`.
12. **CLOSED (2026-09-28)** — mailchimp.members watermark hardened: the
   cursor is now the composite `last_changed|id` (the zoom and s3 sources'
   pattern), so members changed in the same second as the last fired
   member are no longer skipped until something else bumps the watermark.
   Tests: `tests/test_round_mailchimp_drive.py` (same-second regression
   pin).
13. **CLOSED (2026-09-28)** — Multi-user roles v1: authorization grows from
   a single operator allowlist plus per-connection grants to a named user
   role model. A DynamoDB store (`RoleAssignmentsTable`, one row per DTC
   subject or email, in `src/dapier/auth/roles.py`) carries a role —
   `viewer` (read-only console: overview, runs, audit, designer reads,
   discovery, trigger samples), `editor` (also workflow editing/testing,
   replays and cancels, storage writes), `operator` (the historical
   capability set: also connections, credentials, grants, tokens, trigger
   definitions), `admin` (also user management) — plus an optional display
   name and a `disabled` flag that denies an account everywhere. Resolution:
   a stored row wins, narrowing an allowlisted operator to viewer or
   widening a non-allowlisted identity into a band; with no stored row the
   `OPERATOR_EMAILS`/`OPERATOR_SUBJECTS` allowlist stays authoritative —
   answering `admin` while the store is empty, so today's operators keep
   full power and can bootstrap the first admin, and `operator` once any row
   exists — which makes an empty table reproduce the pre-roles gate bit for
   bit, and store failures degrade to the allowlist verdict rather than
   failing a request. The last active admin cannot be demoted, disabled, or
   removed (the API refuses with 409). Enforcement is centralized in the
   auth layer: the console dispatcher gates every route through
   `session.require_role(event, roles.minimum_for_route(method, path))`, the
   agent API through `require_operator`'s `minimum_for_action` band, both
   sharing `roles.satisfies(roles.effective_role(payload))`. Surfaces
   (parity): console `GET/POST /api/admin/users` plus
   `DELETE /api/admin/users/{subject}` behind a Users view (list, set-role
   dialog with display name and disabled flag, remove with confirm);
   `dapier users list|set-role|remove` over the operator- and admin-gated
   `/api/agent/users`; every mutation audited as `users.set-role` /
   `users.remove`. Tests: `tests/test_roles.py` (domain semantics, dispatcher
   bands, bootstrap-then-narrow transitions, last-admin guard, disabled and
   API-token denials, agent users API), `tests/test_users_cli.py` (command
   surface, confirmations, exit codes). Shared workspaces and invitations
   remain future work.
14. **CLOSED (2026-09-28)** — find-or-create breadth and the mailchimp
   unsubscribe staple. `mailchimp_unsubscribe_member` PATCHes the member's
   status to unsubscribed — the reversible counterpart of the permanent
   remove (upsert's `status_if_new` only applies to members the audience
   does not have yet, so it could not unsubscribe an existing one); a
   missing member is `{unsubscribed: false}`, not an error.
   `create_if_missing` now covers every find with a meaningful create:
   `mailchimp_find_member` upserts on a miss (`status` applies as
   status-if-new, optional merge fields, `created: true` in the output —
   Zapier's Find or Create Member), and `slack_find`'s channel branch
   creates a missed name via `conversations.create` (`is_private` honored;
   Zapier's Find or Create Channel — a user miss stays a miss), joining
   the existing sheets/dropbox/zoom finds. Engine runners, registry
   entries and designer catalog mirrors; tests:
   `tests/test_mailchimp_actions.py`, `tests/test_find_slack_dropbox.py`.

14. **CLOSED (2026-09-28)** — Workflow templates v1: a workflow flagged
   `template: true` (the flag rides the workflow YAML, set through
   `api_template_flag`) joins the gallery that `api_templates` serves —
   the bundled starters in `workflows/template-*.yaml` plus anything an
   operator published, the published overlay winning per id. Forking is
   `api_apply_template`: the flagged workflow loads under a new
   `<template-id>-copy` id and goes through the same commit-and-publish
   path as a save. All three surfaces: the designer's "Start from a
   template" gallery (console bundle rebuilt), `GET/POST
   /api/admin/designer/templates*`, the operator-gated `/api/agent/designer/templates*`
   trio, and `dapier templates list|apply|publish|unpublish`. Tests:
   `tests/test_designer.py` (store + admin routes), `tests/test_cli.py`
   (command surface). Cross-account transfer still needs the multi-user
   model (item 13); marketplace-style sharing beyond this deployment
   remains future work.
15. **CLOSED (2026-09-28)** — Designer editor polish: undo/redo over a
   per-session draft timeline (`designer/src/history.ts`, pure module —
   500ms sliding coalescing per editing group, 50-entry cap, reset on
   open and after a successful save; Ctrl/Cmd+Z, Ctrl/Cmd+Shift+Z /
   Ctrl+Y, toolbar buttons), step duplication (fresh id, `-copy`/`(copy)`
   suffixes, inserted after the original), cross-workflow step copy/paste
   (localStorage clipboard, pasted steps validate like manual ones), and
   a "?" shortcuts overlay. Editor-session state only — no API surface,
   so under the parity rule's presentational exception.
16. **CLOSED (2026-09-28)** — Zoom round completion, behind item 11:
   `zoom_delete_meeting` (DELETE /meetings/{id}, optional occurrence_id
   for recurring scoping, output `{deleted, meeting_id}`) and
   find-or-create on `zoom_find_meeting` (`create_if_missing` reuses the
   create path's payload rules; a hit or a meeting-id lookup never
   creates; output gains `created`). Find-or-create verdicts elsewhere:
   `slack_find_user` (no user-create API), `drive_find_file` (a miss
   composes with drive_upload_file), `s3_find` (an empty object is
   storage noise) — deliberately skipped, documented in
   `docs/connector-coverage-audit.md`. Tests: `tests/test_zoom_delete_meeting.py`,
   `tests/test_slack_drive_media.py`, `tests/test_find_media.py`.

Reviewed 2026-09-27 against committed HEAD (`97b2a2c`) plus the in-flight working
tree (trigger discovery and designer changes are being built separately; noted
where they land). Read alongside `docs/architecture-review.md`, which covers the
registry/execution refactor; this document covers the *product loop* gaps beyond
the discovery/replay/test work already underway.

## 1. The Zapier loop, mapped to dapier today

| Zapier concept | Dapier equivalent | Status |
| --- | --- | --- |
| Trigger connectors (webhook, email, schedule, polling) | hook/email/schedule/poll triggers (`src/dapier/triggers/`), registry chips (`connectors/registry.py`) | have |
| Trigger sample discovery ("find recent data") | sample for every chip (`connectors/trigger_discovery.py`); options discovery for every registry listing, invariant-tested (`tests/test_options_breadth.py`); dispatch shared by `/api/{agent,admin}/discover` | have |
| Action catalog with field schemas | `registry.Action.fields`, served by `GET /api/catalog` (`connectors/registry.py:146`) | have |
| Search / find-record actions (lookup, create-if-missing) | 14 find actions with `create_if_missing` (e.g. `sheets_find_row`, `dropbox_find`, `slack_find`, `zoom_find_meeting`, `mailchimp_find_member`) | have |
| Multi-step workflows with data mapping | `steps` context + formatters (`engine/actions/templating.py:38`), `http_request` output capture (`connectors/webhook.py:44`) | have |
| Filters and branching (Paths) | `filter`/`condition`/n-way `paths` on the full trigger-filter operator set; rules read earlier `steps` outputs (`engine/logic.py`, `matching._matches_filter`) | have |
| Looping | `for_each` (`engine/logic.py:275`) | have |
| Delay / "schedule after" / requeue-until | `RunSuspended` requeue-until (`engine/logic.py` `_run_delay`, `engine/worker.py` `_resume_run`) | have |
| Digests / batching | `digest_add`/`digest_flush` + `digest` logic step (`connectors/digest.py`, `engine/actions/digests.py`) | have |
| Per-step error handling / on-failure action | `on_error`/`on_fail` (`engine/logic.py` `_handle_error`, `connectors/registry.py` validation) | have |
| Retry/backoff policy per action | workflow-level `retry` redrive + per-step `autoretry` with exponential backoff (`engine/worker.py`, `engine/logic.py`) | have |
| Task history (runs, step I/O) | EXECUTIONS_TABLE + run grouping (`api/runs.py`) | have |
| Replay a task | `api_replay` re-injects the envelope onto the queue (`api/runs.py:190`) | have |
| Test before publish (dry-run/execute) | `engine/dryrun.py`, designer Test panel, `wf test` CLI | have (stale runner list, below) |
| Field mapping autofill from sample data | trigger-sample endpoint feeds the inspector and Test step (`api/runs.py` `api_trigger_sample`, `engine/dryrun.py` `test_step`) | have |
| Workflow versioning / rollback | `#v<n>` version records in PUBLISHED_WORKFLOWS_TABLE (`triggers/published_workflows.py`); versions list + rollback on console, CLI, and API | have |
| Find my zap (cross-workflow search) | `?q=` search over workflows and run content (`api/overview.py` `_workflow_matches`, `api/runs.py` `api_list`) | have |
| Task usage metering | task counting + monthly rollup (`engine/usage.py`, `dapier usage`); no plan caps yet | have (metering; caps open) |
| Error alerting to the user | opt-in email per failed run + error digest (`engine/notify.py`, `error_digest.py`) | have |
| Save-time input validation | required/unknown keys, template syntax, field types (`connectors/registry.py` `validate_action_chain`, `validate_field_types`) | have |
| Sub-zaps (call another workflow) | `run_workflow` step (`connectors/subworkflow.py`) | have |
| Data stores (Zapier Storage API) | `storage_get/set/delete/find` + storage API + console view (`connectors/storage.py`, `api/storage.py`, `views/storage.js`) | have |
| Outbound webhooks with signing/retry | HMAC signing + typed `HttpError` + per-step `autoretry` (`engine/actions/webhook.py`, `engine/logic.py`) | have |
| Templates / shared zaps (marketplace) | `template: true` flag rides the workflow YAML; gallery + apply (fork) + publish/unpublish on console (designer), CLI (`dapier templates`), and API (`/designer/templates*`); bundled starters in `workflows/template-*.yaml` | have (v1: single-tenant gallery; cross-account transfer still needs multi-user) |

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

### Connector coverage matrix (2026-09-28 audit)

Every capability cell is backed by a registry entry, verified by importing
`connectors` and dumping `registry.catalog()` +
`trigger_discovery.trigger_discovery_catalog()`. "—" means genuinely
not-applicable (no connection record, no remote listing), not missing.

Trigger connectors (palette chips + sample pull):

| connector | events | sample pull | trigger options | action pickers | conn test | find actions |
|---|---|---|---|---|---|---|
| email | message.received | ✓ | — | — | — (no connection record) | — (send only) |
| youtube | video.published | ✓ | — (push, no trigger fields) | playlists | youtube | find video, find playlist videos, upload video (needs `youtube.upload` scope — see docs/connectors/google.md) |
| dropbox | file.created/updated/deleted | ✓ per event | folders | files, folders, search | dropbox | find file or folder |
| zoom | recording.completed, recording.transcript_completed, meeting.started, meeting.ended | ✓ per event | meetings | meetings, recordings | zoom | find meeting, find recording |
| slack | message.received, app.mention, reaction.added, member.joined | ✓ per event | channels | channels, users, messages | slack | find user or channel (create-if-missing on channels), find user by email, find message, update message, add reaction, pin, invite users, DM, create channel, set topic/purpose, upload file, schedule, add reminder |
| telegram | message.received, channel_post.received | ✓ per event | chats | chats | telegram | send, send photo, send document, send poll, find chat |
| renderer | job.completed | ✓ | — | — | — (internal) | — |
| schedule | schedule.triggered | ✓ | — | — | — | — |
| poll | item.new | ✓ | — | — | — | — |
| custom (webhook ingress) | freeform | ✓ | — | — | — | — |

Action-only providers (credential-backed, no trigger chip):

| provider | action pickers | conn test | find actions |
|---|---|---|---|
| google (Sheets + Drive) | spreadsheets, worksheets, columns, rows, files, folders | google | find/lookup/update row, find file |
| s3 / aws keys | buckets, objects | aws | find object |
| mailchimp | audiences, members | mailchimp | find member, upsert member |

Everything else (code/js, logic steps, storage, digest, delay, webhook and
http actions, run_workflow) is internal or generic and needs no remote
listing; `dataops` is action-side intake, so its sample pull is for test
panels only and it deliberately has no palette chip.

One hole the audit closed: **telegram had no palette chip** — its hook
trigger, sample pull, chat options, connection test, and find-chat action
all existed, but `CONNECTORS` never listed it, so the designer's trigger
palette and `GET /api/catalog` had no Telegram entry. Added to
`connectors/triggers.py` (`message.received`, like the hook engine's
`TELEGRAM_EVENT`); `tests/test_trigger_samples.py` now asserts every
sample-discoverable connector has a chip. The webhook-ingress sample stays
keyed `webhook` — it surfaces as the Custom chip.

### Open findings from the 2026-09-28 product-loop audit (ranked)

1. **CLOSED (2026-09-28)** — replay-from-step: `runs.api_replay(run_id,
   from_step=<top-level step id>)` reuses the worker's own park-and-continue
   path — a synthetic `dapier_resume` envelope carries the workflow's tail
   from the chosen step plus the recorded outputs of everything before it
   (seeded `step_outputs`, so templates still read `{steps.<id>.output.*}`),
   and a fresh event id lands the rerun in history tied to the original by
   `correlation_id`. Full replay (re-injected trigger) stays the default;
   the early steps never re-fire on a targeted retry. On every surface:
   `POST /api/{admin,agent}/runs/{id}/replay` with a `from_step` body
   (audited as `runs.replay-from-step`), `dapier runs replay --from-step`,
   and a per-step "Replay from here" button in the console run dialog.
   Refusals are explicit: workflow gone (404), disabled (409), a step name
   nothing records or defines (404), or a recorded step that is not a
   top-level step of the current definition (409). Tests:
   `tests/test_runs.py`, `tests/test_agent_api.py`, `tests/test_admin.py`,
   `tests/test_cli.py`.
2. **CLOSED (2026-09-28)** — run-history content search: `?q=` on
   `GET /api/{admin,agent}/runs` does a case-insensitive substring match
   over each run's recorded step data (input, output, error) and ids inside
   the same bounded scan window the list already walks, so "which run
   carried order #1234" answers from history; composes with the
   workflow/status/time filters and keyset paging, and `paging.filtered`
   stays honest. Wired to `dapier runs list --search` and the console
   Runs view's debounced search box. Tests: `tests/test_runs.py`,
   `tests/test_agent_api.py`, `tests/test_cli.py`.
3. **CLOSED (2026-09-28)** — listings carry an offline `health:
   ok|expired` + `token_expires_at` computed from the stored OAuth token
   (`records.public_view(item, stored)`, fed by `tokens.stored_value` on
   `/api/agent/connections` and `/api/admin/overview`). The console
   Connections view turns expired tokens into a live "needs reconnection"
   row with a Reconnect action — counts, filter, and sort follow — and
   `dapier connections list|show` print HEALTH/EXPIRES. Tests:
   `tests/test_agent_api.py`, `tests/test_admin.py`, `tests/test_cli.py`.
4. **CLOSED (2026-09-28)** — the daily operator error digest is scheduled
   (`ErrorDigestFunction`, cron 07:00 UTC): `error_digest.py` renders the
   `api_summary` rollup into one SES email to the operator address, a
   zero-failure window skips the send, and function errors page the alarm
   chain (`ErrorDigestErrorAlarm`). Send-now parity: operator-gated
   `POST /api/{admin,agent}/errors/digest` (audited `errors.send-digest`
   on real sends), `dapier errors send-digest`, and the console
   Failed-runs panel's "Send digest now". Tests: `tests/test_error_digest.py`.
5. **CLOSED (2026-09-28)** — workflow delete on every surface:
   `designer_store.api_delete` unpublishes the live item (the engine stops
   matching on the next event), then removes `workflows/<file>` in one
   atomic git tree-delete commit (`commit_delete`, a `sha: null` entry
   against the base tree — the same mechanism a rename uses), refusing 409
   while any run is parked on a delay (a resume would dangle into a
   definition that no longer exists; the operator cancels or waits them
   out). Run history is untouched — deleting stops the automation, not the
   audit trail. A git failure after a live delete reports
   `git_sync_error` and still counts as deleted. Surfaces: audited
   `DELETE /api/{admin,agent}/designer/workflows/<file>` (console Delete
   button with confirm), `dapier workflows delete [--yes]` (warns when the
   commit failed). Tests: `tests/test_workflow_delete_tags.py`.
6. **CLOSED (2026-09-28)** — webhook triggers answer sync: `response: {mode:
   ack|challenge|sync, template}` on the stored hook (webhook kind only —
   Telegram keeps its fast fixed ack), validated at save on both dispatchers
   through `build_item`. `ack` (the default) is the old fixed 202;
   `challenge` answers verification handshakes (query `challenge` /
   `hub.challenge`, falling back to the body's `challenge`) with plain-text
   200 and runs nothing; `sync` runs the matched workflow inline in the
   ingress — the production path (step leases, run history, task usage,
   failure notify, workflow retry) via `worker.execute` with the worker's own
   attempt hooks — and answers 200 with the `template` rendered against
   `{trigger.*}` / `{steps.<id>.output.*}` (any JSON shape; default body
   `{"ok", "workflow", "steps", "event_id"}`), 202 `{"suspended": true,
   "resume_at": …}` when a delay parks the run (the continuation is enqueued
   exactly as the worker parks it), or 500 with the delivery's dedupe claim
   released (a provider retry re-executes) when the chain fails. Optional
   `status` (2xx, default 200) sets the success code and `budget_seconds`
   (1-25, default 10) the inline run's wall-clock budget. Dedupe composes: a
   retried delivery is answered, not re-run. Sync is a single-workflow
   contract — zero or several matching workflows fall back to the ack path
   (202, with a `reason`) — and the budget is enforced between steps, never
   mid-step: on overrun the un-run tail is enqueued under the same event id
   (the completed steps' leases dedupe the worker's replay) and the caller
   gets 202 `{"reason": "budget"}`, so the run still finishes out of band;
   longer chains belong in ack mode. Surfaces: the console hook dialog's
   Response select + template JSON field, `dapier hooks save` body files
   plus `--sync-response` (shown by `hooks show`). Tests:
   `tests/test_hook_sync_response.py`, `tests/test_sync_response.py`,
   `tests/test_webhook_sync.py`.
7. **CLOSED (2026-09-28)** — trigger dedupe: a TTL-bounded seen-id store
   (`triggers/seen.py`, one item per scope in the cursors table, 30-day
   expiry, 1000-entry cap, atomic `claim`/`forget`) closes both halves.
   `next_cursor` polls now skip items whose stable id was already published
   (providers recycle pages) and park the provider's continuation cursor —
   never an item id — once a page drained; watermark polls are unchanged
   (the cursor already dedupes). Webhook triggers take an optional
   `dedupe_path` (dot-path to a stable delivery id in the payload; console
   Triggers form and CLI `hooks save` both set it): a retried delivery
   claims the same event id, answers `202 duplicate`, and never publishes —
   a publish failure releases the claim so the retry still delivers, and a
   seen-store outage publishes anyway (the stable id still dedupes
   downstream through run grouping and step leases). Telegram triggers
   dedupe by default on `update_id`, the provider's own delivery identity.
   Tests: `tests/test_seen_store.py`, `tests/test_poll_dedupe.py`,
   `tests/test_poll_triggers.py`, `tests/test_hook_dedupe.py`.
8. **CLOSED (2026-09-28)** — tags: a workflow carries up to 20
   short labels in its YAML (`tags:`, validated by `_validate_tags`), set
   through `PUT /api/{admin,agent}/designer/workflows/<file>/tags`
   (audited `workflow.tags`, published with cause "tags", committed
   best-effort like the toggle), `dapier workflows tags <file> --tags
   a,b | --clear`, and a console Tags prompt per row. Tags are normalized
   to lowercase, deduped case-insensitively, and bounded (20 × 64 chars).
   Both list endpoints narrow with `?tag=` (case-insensitive) — the CLI
   mirrors it with `workflows list --tag`, and the console's Workflows
   view gets a tag dropdown plus tag chips per row. Tags travel with
   saves, deploys, duplicates, and rollbacks because they live in the
   definition. Bulk enable/disable is also in: audited
   `POST /api/{admin,agent}/designer/workflows/bulk`
   (`{"ids": [...], "action": "enable|disable"}` with per-id results,
   refusing parked-delay workflows like the single toggle) and
   `dapier workflows on|off <file>...` taking multiple files in one call.
   And folders close the slice: a flat `folder:` top-level YAML key
   (`_validate_folder` — ≤64 chars, never a path; Zapier folders are
   flat), `PUT /api/{admin,agent}/designer/workflows/<file>/folder`
   (audited `workflow.folder`, cause "folder"), `dapier workflows folder
   <file> --set "Name" | --clear` and `workflows list --folder`, `?folder=`
   on both list routes plus the overview payload (a `workflow_folders`
   aggregate beside the tags one), and a console Folder dropdown + folder
   chips + per-row Folder prompt. Tests: `tests/test_workflow_bulk.py`,
   `tests/test_workflow_folders.py`, `tests/test_workflow_tags.py`,
   `tests/test_workflow_delete.py`, `tests/test_workflow_delete_tags.py`.

### Round 5 (2026-09-28): fresh product-loop audit — coverage re-verified, polish landed

A read-only audit re-verified the connector coverage matrix against a live
`registry.catalog()` / `trigger_discovery_catalog()` import: every cell is
backed by a current registry entry, and the designer mirror's action/logic/
filter sets match the registry exactly (one drift caught mid-flight — the
mailchimp trigger chip landed on the Python side before the mirror; chips
always need the `make designer-console` rebuild). All four earlier
"Surprises" were re-checked and confirmed fixed with line evidence: dryrun
derives runners from the registry, a live step lease now requeues the
redelivery instead of skipping it (`LeaseBusy`), the `webhook` action returns
the response body, and poll failures notify the operator.

Polish findings from that audit, landed:

- **OAuth verify-failure refresh fallback** — `tokens.get_access_token` tries
  one `refresh_and_store` when a locally-fresh token fails provider
  verification (server-side revocation/rotation, clock drift), instead of
  wedging every workflow on the connection until natural expiry; the write is
  version-conditional and the replacement token is verified and bind-checked
  before storing. Tests: `tests/test_token_refresh_fallback.py`.
- **429/503 Retry-After under autoretry** — `webhook`/`http_request` failures
  raise a typed `HttpError` carrying `status` (plus a parsed seconds
  `retry_after` where the response headers are readable); an opted-in
  `autoretry` sleeps `min(max_seconds, retry_after)` plus jitter on 429/503
  instead of blind backoff. Steps without `autoretry` are unchanged, and
  provider 4xx/5xx on the default-urllib paths now surface as `HttpError`
  rather than a raw `urllib.error.HTTPError`. Tests: `tests/test_retry_after.py`.
- **`list_versions` scan capped** at `SCAN_LIMIT` like its sibling
  `load_items` (was an unbounded full-table scan on every versions read and
  rollback). Tests: `tests/test_published_workflows.py`.
- **Overview executions block ordered by `started_at`** — the old sort was
  reverse string order on `{workflow}:{action}:{event}` ids, which is not
  chronological, so the block could show stale rows beside the correctly
  ordered runs list. Tests: `tests/test_admin.py`.
- **Error summary/digest honesty about the 200-run cap** — the summary
  payload carries `bounded`/`cap` when the failed-run scan hit the limit and
  the daily digest email renders a "most recent N failed runs are counted"
  caveat, so a >200-failure day no longer reads as exactly 200. Tests:
  `tests/test_error_digest.py`.

Still open (ranked, from the same audit):

1. **CLOSED (2026-09-28)** — Mailchimp webhook lifecycle: a mailchimp hook
   trigger now binds its audience (`list_id`, plus optional subscribed
   `events` and a mailchimp `connection_id`) and drives
   Mailchimp's `POST/DELETE /lists/{id}/webhooks` server-side — save
   subscribes the hook URL (replacing any webhook already registered under
   it) with every source subscribed, and delete/disable unsubscribes by
   listing the audience's webhooks and deleting ours by id. Best-effort on
   both ends: a Mailchimp failure never blocks the stored change, it comes
   back as `warnings` on the response (surfaced by `dapier hooks
   save/delete`); the shared `mailchimp` credential or the bound
   connection's key authenticates, datacenter derived from the key suffix.
   Deliveries match for real: the intake publishes the URL's hook name in
   the event data, a trigger's stored workflow fans out to one trigger spec
   per subscribed type (matching is exact on event, so the old single
   `message.received` spec never fired), and the trigger view/CLI carry no
   bearer hint — Mailchimp sends no auth headers.
   Tests: `tests/test_mailchimp_webhook_lifecycle.py`,
   `tests/test_hook_triggers.py`, `tests/test_mailchimp_intake.py`.
2. **CLOSED (2026-09-28)** — formatter edges: `date_format` takes an
   optional IANA zone as a `@`-suffix on its raw argument
   (`{v|date_format:%Y-%m-%d %H:%M@Europe/Berlin}` — zoneinfo conversion,
   offset-less values read as UTC, unknown zone renders empty per the
   never-raise rule); new zero-arg `time_until` relative formatter
   (`in 3h`, `5m ago`, `just now` within a minute; parses via
   `logic.parse_moment`); `number_format:currency:EUR[:decimals]`
   (€/$/£ table, unknown codes fall back to `1,234.56 JPY` shape),
   validated mode-aware at save time. Tests: `tests/test_templating.py`
   FormatterEdgeTests.
3. **CLOSED (2026-09-28)** — all-workflows export bundle:
   `dapier workflows export --all [-o out.zip]` lists via the agent
   designer-list endpoint, fetches each workflow's canonical YAML through
   the same single-export endpoint, and zips them as flat `<file>.yaml`
   (empty list refuses politely rc 2; a workflow without YAML is skipped
   with rc 5 and the rest bundled). Console parity holds through the
   existing per-workflow Download YAML — both drive the same API. Tests:
   `tests/test_cli.py`.

4. **CLOSED (2026-09-28)** — run-history CSV export, three surfaces over
   one domain function (`runs.api_export`, modeled on the audit trail's
   export): the list's filters (`workflow`, `status` incl. the
   success/problems aliases, `since`/`before`, content search `q`),
   newest first, capped at `max_rows` (default 500, max 2000,
   `truncated` flags the clip), returning
   `{filename, count, truncated, csv}` with one row per run
   (`run_id … error`, booleans as true/false). Audited like every bulk
   read: `runs.export` lands in the operator trail. Surfaces: audited
   `GET /api/admin/runs/export`, operator-gated
   `GET /api/agent/runs/export`, `dapier runs export
   [--workflow|--status|--since|--before|--search|--max-rows|--out]`
   (writes the server's suggested filename by default), and the console
   Runs view's Export CSV button (downloads the filtered history at the
   2000-row cap). Tests: `tests/test_runs.py`, `tests/test_admin.py`,
   `tests/test_agent_api.py`, `tests/test_cli.py`.

5. **CLOSED (2026-09-28)** — YouTube subscribe-on-save: saving, toggling,
   renaming, or rolling back a workflow whose trigger is `youtube /
   video.published` syncs the WebSub hub immediately
   (`youtube_subscriptions.reconcile`, called best-effort from the designer
   store): newly watched channels are subscribed on save, and channels the
   change orphaned are unsubscribed — but only when no other enabled
   workflow still names the channel, and only the delta, so an edit that
   leaves the channel list alone never rings the hub. Disable and delete
   unsubscribe the same way; every hub failure comes back as `warnings` on
   the save/toggle/bulk/delete response (surfaced by `dapier workflows
   save/on/off/delete`) instead of blocking the change, and the 5-day
   renewal schedule remains the reconciliation pass for anything missed.
   Deploy note: the API function now also needs `YOUTUBE_CALLBACK_URL` in
   template.yaml. Tests: `tests/test_youtube_on_save.py`,
   `tests/test_youtube_subscriptions.py`.
6. **CLOSED (2026-09-28)** — Dropbox and Slack action breadth (Zapier's
   file-management and channel-management sets). Dropbox gains
   `dropbox_create_folder` (files/create_folder_v2; an existing folder
   errors — `dropbox_find` with `create_if_missing` stays the find-or-create
   path), `dropbox_move` (files/move_v2 — also Zapier's Rename File: a move
   within the same folder under a new name), and `dropbox_copy`
   (files/copy_v2); both transfer actions render both paths from the event
   and take `autorename` to suffix on conflict, outputting
   `{moved|copied, item}`. Slack gains `slack_dm`
   (conversations.open + chat.postMessage — chain `slack_find_user` and pass
   `{steps.<id>.output.user.id}`), `slack_create_channel`
   (conversations.create; the name is normalized to what Slack accepts —
   lowercase, spaces to hyphens, illegal characters dropped, ≤80 chars — so
   templated names never fail on a stray character), `slack_set_topic` and
   `slack_set_purpose` (conversations.setTopic/setPurpose); alongside the
   update-message and add-reaction actions these complete Zapier's core
   Slack set. All ride the registry (designer mirror + console bundle
   rebuilt) and the existing run/Test-step surfaces — no new endpoints.
   Tests: `tests/test_dropbox_slack_actions.py`.
7. **CLOSED (2026-09-28)** — Slack trigger variety: the Events intake no
   longer folds everything into `message.received`. `slack_events.event_name`
   maps each subscribed Slack type to its own workflow-facing event —
   `message.*` keeps `message.received`, `app_mention` publishes
   `app.mention`, `reaction_added` publishes `reaction.added`,
   `member_joined_channel` publishes `member.joined` (anything else still
   answers `accepted: false`; bot posts still never publish) — and the
   envelope flattens `reaction`, `item_ts` and `inviter` beside the message
   fields. The Slack chip declares all four events, the sample pull serves
   one documented delivery per event (`message.received` stays the live
   channels walk; a reaction or join never shows in history, so the others
   fall back to recorded runs then the documented delivery), and the
   designer mirror's connector catalog carries the same four. Compatibility
   note: `app_mention` no longer also fires `message.received` — workflows
   that relied on that re-filter on `app.mention`
   (docs/connectors/slack.md documents the setup and the mapping).
   Tests: `tests/test_slack_event_variety.py`, `tests/test_slack_events.py`
   (per-event sample pull), `tests/test_slack_actions.py`.
8. **CLOSED (2026-09-28)** — Mailchimp and Google Drive parity round: the
   audience and the drive gain their missing Zapier staples on all
   surfaces. Mailchimp gains `mailchimp_remove_member` (DELETE on the
   email's md5 path; a missing member is `{removed: false}`, not an
   error) and `mailchimp_tag_member` (POST `members/{md5}/tags` — Add
   applies the tag, Remove sets it inactive), and its first poll source:
   `mailchimp.members` lists one audience through the Marketing API with
   the stored key (a named `connection_id` or the shared `mailchimp`
   credential — no OAuth required) and publishes `mailchimp`/`member.new`
   on a `last_changed` watermark — seeded first fire (the roster is
   history, not news), oldest-first emission, and the seen-set keeping a
   profile edit from re-firing an already-seen member. Google Drive gains
   the change-feed sources Zapier's Updated/Deleted File triggers need —
   `google-drive.updates` (`file.updated`, folder-scoped when `folder_id`
   is stored) and `google-drive.deletions` (`file.deleted`, always
   Drive-wide: a removal change carries a fileId only) — sharing one
   `changes.list` fetch whose opaque page-token cursor parks only once a
   page drains, so the budget and seen-set machinery work unchanged; plus
   `drive_share_file` (permissions.create — user/group/domain/email
   grantee) and `drive_copy_file` (files.copy). The chips declare
   `member.new` / `file.updated` / `file.deleted`, the sample pulls serve
   each (stored-poll live pulls first: Drive's changes sources sample
   against the parked cursor read-only, Mailchimp's against the epoch
   watermark; fallbacks key on the poll's event), and the designer mirror
   carries the actions and events (console bundle rebuilt). Tests:
   `tests/test_round_mailchimp_drive.py`, `tests/test_mailchimp_actions.py`,
   `tests/test_mailchimp_poll.py`, `tests/test_drive_changes.py`.
9. **CLOSED (2026-09-28)** — Slack gets its no-app poll source:
   `slack.messages` lists one channel through `conversations.history` (two
   pages of 200, watermark = the newest ts) with the stored bot token and
   publishes the same `message.received` event the Events intake does —
   scheduled polling feeds the identical pipeline with no Slack app and no
   Event Subscriptions setup; the first fire seeds without emitting, and
   bot posts and membership subtypes never flatten (the intake's rule).
   The chip needs nothing new; the sample pull walks the channel live.
   Tests: `tests/test_round_slack.py`.
10. **CLOSED (2026-09-28)** — Sheets and Telegram action staples:
   `sheets_delete_row` (worksheet title → sheetId, then batchUpdate
   deleteDimension; pairs with sheets_lookup_row's row output) and
   `sheets_create_spreadsheet` (spreadsheets.create, optional header row
   written through the existing values.update path — the output
   spreadsheet_id/url feed a follow-up append); `telegram_send_photo` and
   `telegram_send_document` (Bot API multipart, chat_id falling back to
   the triggering chat; media comes from source_url — dapier fetches it
   itself, so Telegram need not reach it — or a staged source_s3
   {bucket, key}). Tests: `tests/test_round_sheetstelegram.py`.
11. **CLOSED (2026-09-28)** — YouTube, S3 and Zoom action staples plus
   http_request response visibility: `youtube_add_to_playlist`
   (playlistItems.insert — directly serves the DTC channel workflow) and
   `youtube_update_video` (videos.update part=snippet; title required —
   YouTube replaces the snippet whole, and omitting category clears it);
   `s3_read_object` (GetObject staged like the dropbox read),
   `s3_presign_url` (presigned GET, one hour by default up to SigV4's
   seven-day ceiling — the dropbox get_temp_link symmetry) and
   `s3_delete_object` (idempotent DeleteObject); `zoom_update_meeting`
   (PATCH /meetings/{id} with only the filled fields — an empty update is
   rejected) and `zoom_add_registrant` (POST registrants; join_url is
   unique per registrant). `http_request` and the webhook action surface
   `response_headers` (lowercased) whenever the transport surfaced them —
   legacy two-key outputs unchanged. Tests: `tests/test_round_yts3zoom.py`,
   `tests/test_http_response_headers.py`.
12. **CLOSED (2026-09-28)** — mailchimp.members watermark hardened: the
   cursor is now the composite `last_changed|id` (the zoom and s3 sources'
   pattern), so members changed in the same second as the last fired
   member are no longer skipped until something else bumps the watermark.
   Tests: `tests/test_round_mailchimp_drive.py` (same-second regression
   pin).
13. **CLOSED (2026-09-28)** — Multi-user roles v1: authorization grows from
   a single operator allowlist plus per-connection grants to a named user
   role model. A DynamoDB store (`RoleAssignmentsTable`, one row per DTC
   subject or email, in `src/dapier/auth/roles.py`) carries a role —
   `viewer` (read-only console: overview, runs, audit, designer reads,
   discovery, trigger samples), `editor` (also workflow editing/testing,
   replays and cancels, storage writes), `operator` (the historical
   capability set: also connections, credentials, grants, tokens, trigger
   definitions), `admin` (also user management) — plus an optional display
   name and a `disabled` flag that denies an account everywhere. Resolution:
   a stored row wins, narrowing an allowlisted operator to viewer or
   widening a non-allowlisted identity into a band; with no stored row the
   `OPERATOR_EMAILS`/`OPERATOR_SUBJECTS` allowlist stays authoritative —
   answering `admin` while the store is empty, so today's operators keep
   full power and can bootstrap the first admin, and `operator` once any row
   exists — which makes an empty table reproduce the pre-roles gate bit for
   bit, and store failures degrade to the allowlist verdict rather than
   failing a request. The last active admin cannot be demoted, disabled, or
   removed (the API refuses with 409). Enforcement is centralized in the
   auth layer: the console dispatcher gates every route through
   `session.require_role(event, roles.minimum_for_route(method, path))`, the
   agent API through `require_operator`'s `minimum_for_action` band, both
   sharing `roles.satisfies(roles.effective_role(payload))`. Surfaces
   (parity): console `GET/POST /api/admin/users` plus
   `DELETE /api/admin/users/{subject}` behind a Users view (list, set-role
   dialog with display name and disabled flag, remove with confirm);
   `dapier users list|set-role|remove` over the operator- and admin-gated
   `/api/agent/users`; every mutation audited as `users.set-role` /
   `users.remove`. Tests: `tests/test_roles.py` (domain semantics, dispatcher
   bands, bootstrap-then-narrow transitions, last-admin guard, disabled and
   API-token denials, agent users API), `tests/test_users_cli.py` (command
   surface, confirmations, exit codes). Shared workspaces and invitations
   remain future work.
14. **CLOSED (2026-09-28)** — find-or-create breadth and the mailchimp
   unsubscribe staple. `mailchimp_unsubscribe_member` PATCHes the member's
   status to unsubscribed — the reversible counterpart of the permanent
   remove (upsert's `status_if_new` only applies to members the audience
   does not have yet, so it could not unsubscribe an existing one); a
   missing member is `{unsubscribed: false}`, not an error.
   `create_if_missing` now covers every find with a meaningful create:
   `mailchimp_find_member` upserts on a miss (`status` applies as
   status-if-new, optional merge fields, `created: true` in the output —
   Zapier's Find or Create Member), and `slack_find`'s channel branch
   creates a missed name via `conversations.create` (`is_private` honored;
   Zapier's Find or Create Channel — a user miss stays a miss), joining
   the existing sheets/dropbox/zoom finds. Engine runners, registry
   entries and designer catalog mirrors; tests:
   `tests/test_mailchimp_actions.py`, `tests/test_find_slack_dropbox.py`.

14. **CLOSED (2026-09-28)** — Workflow templates v1: a workflow flagged
   `template: true` (the flag rides the workflow YAML, set through
   `api_template_flag`) joins the gallery that `api_templates` serves —
   the bundled starters in `workflows/template-*.yaml` plus anything an
   operator published, the published overlay winning per id. Forking is
   `api_apply_template`: the flagged workflow loads under a new
   `<template-id>-copy` id and goes through the same commit-and-publish
   path as a save. All three surfaces: the designer's "Start from a
   template" gallery (console bundle rebuilt), `GET/POST
   /api/admin/designer/templates*`, the operator-gated `/api/agent/designer/templates*`
   trio, and `dapier templates list|apply|publish|unpublish`. Tests:
   `tests/test_designer.py` (store + admin routes), `tests/test_cli.py`
   (command surface). Cross-account transfer still needs the multi-user
   model (item 13); marketplace-style sharing beyond this deployment
   remains future work.
15. **CLOSED (2026-09-28)** — Designer editor polish: undo/redo over a
   per-session draft timeline (`designer/src/history.ts`, pure module —
   500ms sliding coalescing per editing group, 50-entry cap, reset on
   open and after a successful save; Ctrl/Cmd+Z, Ctrl/Cmd+Shift+Z /
   Ctrl+Y, toolbar buttons), step duplication (fresh id, `-copy`/`(copy)`
   suffixes, inserted after the original), cross-workflow step copy/paste
   (localStorage clipboard, pasted steps validate like manual ones), and
   a "?" shortcuts overlay. Editor-session state only — no API surface,
   so under the parity rule's presentational exception.
16. **CLOSED (2026-09-28)** — Zoom round completion, behind item 11:
   `zoom_delete_meeting` (DELETE /meetings/{id}, optional occurrence_id
   for recurring scoping, output `{deleted, meeting_id}`) and
   find-or-create on `zoom_find_meeting` (`create_if_missing` reuses the
   create path's payload rules; a hit or a meeting-id lookup never
   creates; output gains `created`). Find-or-create verdicts elsewhere:
   `slack_find_user` (no user-create API), `drive_find_file` (a miss
   composes with drive_upload_file), `s3_find` (an empty object is
   storage noise) — deliberately skipped, documented in
   `docs/connector-coverage-audit.md`. Tests: `tests/test_zoom_delete_meeting.py`,
   `tests/test_slack_drive_media.py`, `tests/test_find_media.py`.

Reviewed 2026-09-27 against committed HEAD (`97b2a2c`) plus the in-flight working
tree (trigger discovery and designer changes are being built separately; noted
where they land). Read alongside `docs/architecture-review.md`, which covers the
registry/execution refactor; this document covers the *product loop* gaps beyond
the discovery/replay/test work already underway.

## 1. The Zapier loop, mapped to dapier today

| Zapier concept | Dapier equivalent | Status |
| --- | --- | --- |
| Trigger connectors (webhook, email, schedule, polling) | hook/email/schedule/poll triggers (`src/dapier/triggers/`), registry chips (`connectors/registry.py`) | have |
| Trigger sample discovery ("find recent data") | sample for every chip (`connectors/trigger_discovery.py`); options discovery for every registry listing, invariant-tested (`tests/test_options_breadth.py`); dispatch shared by `/api/{agent,admin}/discover` | have |
| Action catalog with field schemas | `registry.Action.fields`, served by `GET /api/catalog` (`connectors/registry.py:146`) | have |
| Search / find-record actions (lookup, create-if-missing) | 14 find actions with `create_if_missing` (e.g. `sheets_find_row`, `dropbox_find`, `slack_find`, `zoom_find_meeting`, `mailchimp_find_member`) | have |
| Multi-step workflows with data mapping | `steps` context + formatters (`engine/actions/templating.py:38`), `http_request` output capture (`connectors/webhook.py:44`) | have |
| Filters and branching (Paths) | `filter`/`condition`/n-way `paths` on the full trigger-filter operator set; rules read earlier `steps` outputs (`engine/logic.py`, `matching._matches_filter`) | have |
| Looping | `for_each` (`engine/logic.py:275`) | have |
| Delay / "schedule after" / requeue-until | `RunSuspended` requeue-until (`engine/logic.py` `_run_delay`, `engine/worker.py` `_resume_run`) | have |
| Digests / batching | `digest_add`/`digest_flush` + `digest` logic step (`connectors/digest.py`, `engine/actions/digests.py`) | have |
| Per-step error handling / on-failure action | `on_error`/`on_fail` (`engine/logic.py` `_handle_error`, `connectors/registry.py` validation) | have |
| Retry/backoff policy per action | workflow-level `retry` redrive + per-step `autoretry` with exponential backoff (`engine/worker.py`, `engine/logic.py`) | have |
| Task history (runs, step I/O) | EXECUTIONS_TABLE + run grouping (`api/runs.py`) | have |
| Replay a task | `api_replay` re-injects the envelope onto the queue (`api/runs.py:190`) | have |
| Test before publish (dry-run/execute) | `engine/dryrun.py`, designer Test panel, `wf test` CLI | have (stale runner list, below) |
| Field mapping autofill from sample data | trigger-sample endpoint feeds the inspector and Test step (`api/runs.py` `api_trigger_sample`, `engine/dryrun.py` `test_step`) | have |
| Workflow versioning / rollback | `#v<n>` version records in PUBLISHED_WORKFLOWS_TABLE (`triggers/published_workflows.py`); versions list + rollback on console, CLI, and API | have |
| Find my zap (cross-workflow search) | `?q=` search over workflows and run content (`api/overview.py` `_workflow_matches`, `api/runs.py` `api_list`) | have |
| Task usage metering | task counting + monthly rollup (`engine/usage.py`, `dapier usage`); no plan caps yet | have (metering; caps open) |
| Error alerting to the user | opt-in email per failed run + error digest (`engine/notify.py`, `error_digest.py`) | have |
| Save-time input validation | required/unknown keys, template syntax, field types (`connectors/registry.py` `validate_action_chain`, `validate_field_types`) | have |
| Sub-zaps (call another workflow) | `run_workflow` step (`connectors/subworkflow.py`) | have |
| Data stores (Zapier Storage API) | `storage_get/set/delete/find` + storage API + console view (`connectors/storage.py`, `api/storage.py`, `views/storage.js`) | have |
| Outbound webhooks with signing/retry | HMAC signing + typed `HttpError` + per-step `autoretry` (`engine/actions/webhook.py`, `engine/logic.py`) | have |
| Templates / shared zaps (marketplace) | `template: true` flag rides the workflow YAML; gallery + apply (fork) + publish/unpublish on console (designer), CLI (`dapier templates`), and API (`/designer/templates*`); bundled starters in `workflows/template-*.yaml` | have (v1: single-tenant gallery; cross-account transfer still needs multi-user) |

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

### Connector coverage matrix (2026-09-28 audit)

Every capability cell is backed by a registry entry, verified by importing
`connectors` and dumping `registry.catalog()` +
`trigger_discovery.trigger_discovery_catalog()`. "—" means genuinely
not-applicable (no connection record, no remote listing), not missing.

Trigger connectors (palette chips + sample pull):

| connector | events | sample pull | trigger options | action pickers | conn test | find actions |
|---|---|---|---|---|---|---|
| email | message.received | ✓ | — | — | — (no connection record) | — (send only) |
| youtube | video.published | ✓ | — (push, no trigger fields) | playlists | youtube | find video, find playlist videos, upload video (needs `youtube.upload` scope — see docs/connectors/google.md) |
| dropbox | file.created/updated/deleted | ✓ per event | folders | files, folders, search | dropbox | find file or folder |
| zoom | recording.completed, recording.transcript_completed, meeting.started, meeting.ended, meeting.registration_created, webinar.started, webinar.ended, webinar.registration_created | ✓ per event | meetings, past_meetings, recordings, webinars | meetings, past_meetings, recordings, webinars | zoom | find meeting, find recording, find webinar, create meeting + webinar, update meeting + webinar, delete meeting/recording/webinar, add registrant (meeting + webinar), list past participants (meeting + webinar) |
| slack | message.received, app.mention, reaction.added, member.joined | ✓ per event | channels | channels, users, messages | slack | find user or channel (create-if-missing on channels), find user by email, find message, update message, add reaction, pin, invite users, DM, create channel, set topic/purpose, upload file, schedule, add reminder |
| telegram | message.received, channel_post.received | ✓ per event | chats | chats | telegram | send, send photo, send document, send poll, find chat |
| renderer | job.completed | ✓ | — | — | — (internal) | — |
| schedule | schedule.triggered | ✓ | — | — | — | — |
| poll | item.new | ✓ | — | — | — | — |
| custom (webhook ingress) | freeform | ✓ | — | — | — | — |

Action-only providers (credential-backed, no trigger chip):

| provider | action pickers | conn test | find actions |
|---|---|---|---|
| google (Sheets + Drive) | spreadsheets, worksheets, columns, rows, files, folders | google | find/lookup/update row, find file |
| s3 / aws keys | buckets, objects | aws | find object |
| mailchimp | audiences, members | mailchimp | find member, upsert member |

Everything else (code/js, logic steps, storage, digest, delay, webhook and
http actions, run_workflow) is internal or generic and needs no remote
listing; `dataops` is action-side intake, so its sample pull is for test
panels only and it deliberately has no palette chip.

One hole the audit closed: **telegram had no palette chip** — its hook
trigger, sample pull, chat options, connection test, and find-chat action
all existed, but `CONNECTORS` never listed it, so the designer's trigger
palette and `GET /api/catalog` had no Telegram entry. Added to
`connectors/triggers.py` (`message.received`, like the hook engine's
`TELEGRAM_EVENT`); `tests/test_trigger_samples.py` now asserts every
sample-discoverable connector has a chip. The webhook-ingress sample stays
keyed `webhook` — it surfaces as the Custom chip.

### Open findings from the 2026-09-28 product-loop audit (ranked)

1. **CLOSED (2026-09-28)** — replay-from-step: `runs.api_replay(run_id,
   from_step=<top-level step id>)` reuses the worker's own park-and-continue
   path — a synthetic `dapier_resume` envelope carries the workflow's tail
   from the chosen step plus the recorded outputs of everything before it
   (seeded `step_outputs`, so templates still read `{steps.<id>.output.*}`),
   and a fresh event id lands the rerun in history tied to the original by
   `correlation_id`. Full replay (re-injected trigger) stays the default;
   the early steps never re-fire on a targeted retry. On every surface:
   `POST /api/{admin,agent}/runs/{id}/replay` with a `from_step` body
   (audited as `runs.replay-from-step`), `dapier runs replay --from-step`,
   and a per-step "Replay from here" button in the console run dialog.
   Refusals are explicit: workflow gone (404), disabled (409), a step name
   nothing records or defines (404), or a recorded step that is not a
   top-level step of the current definition (409). Tests:
   `tests/test_runs.py`, `tests/test_agent_api.py`, `tests/test_admin.py`,
   `tests/test_cli.py`.
2. **CLOSED (2026-09-28)** — run-history content search: `?q=` on
   `GET /api/{admin,agent}/runs` does a case-insensitive substring match
   over each run's recorded step data (input, output, error) and ids inside
   the same bounded scan window the list already walks, so "which run
   carried order #1234" answers from history; composes with the
   workflow/status/time filters and keyset paging, and `paging.filtered`
   stays honest. Wired to `dapier runs list --search` and the console
   Runs view's debounced search box. Tests: `tests/test_runs.py`,
   `tests/test_agent_api.py`, `tests/test_cli.py`.
3. **CLOSED (2026-09-28)** — listings carry an offline `health:
   ok|expired` + `token_expires_at` computed from the stored OAuth token
   (`records.public_view(item, stored)`, fed by `tokens.stored_value` on
   `/api/agent/connections` and `/api/admin/overview`). The console
   Connections view turns expired tokens into a live "needs reconnection"
   row with a Reconnect action — counts, filter, and sort follow — and
   `dapier connections list|show` print HEALTH/EXPIRES. Tests:
   `tests/test_agent_api.py`, `tests/test_admin.py`, `tests/test_cli.py`.
4. **CLOSED (2026-09-28)** — the daily operator error digest is scheduled
   (`ErrorDigestFunction`, cron 07:00 UTC): `error_digest.py` renders the
   `api_summary` rollup into one SES email to the operator address, a
   zero-failure window skips the send, and function errors page the alarm
   chain (`ErrorDigestErrorAlarm`). Send-now parity: operator-gated
   `POST /api/{admin,agent}/errors/digest` (audited `errors.send-digest`
   on real sends), `dapier errors send-digest`, and the console
   Failed-runs panel's "Send digest now". Tests: `tests/test_error_digest.py`.
5. **CLOSED (2026-09-28)** — workflow delete on every surface:
   `designer_store.api_delete` unpublishes the live item (the engine stops
   matching on the next event), then removes `workflows/<file>` in one
   atomic git tree-delete commit (`commit_delete`, a `sha: null` entry
   against the base tree — the same mechanism a rename uses), refusing 409
   while any run is parked on a delay (a resume would dangle into a
   definition that no longer exists; the operator cancels or waits them
   out). Run history is untouched — deleting stops the automation, not the
   audit trail. A git failure after a live delete reports
   `git_sync_error` and still counts as deleted. Surfaces: audited
   `DELETE /api/{admin,agent}/designer/workflows/<file>` (console Delete
   button with confirm), `dapier workflows delete [--yes]` (warns when the
   commit failed). Tests: `tests/test_workflow_delete_tags.py`.
6. **CLOSED (2026-09-28)** — webhook triggers answer sync: `response: {mode:
   ack|challenge|sync, template}` on the stored hook (webhook kind only —
   Telegram keeps its fast fixed ack), validated at save on both dispatchers
   through `build_item`. `ack` (the default) is the old fixed 202;
   `challenge` answers verification handshakes (query `challenge` /
   `hub.challenge`, falling back to the body's `challenge`) with plain-text
   200 and runs nothing; `sync` runs the matched workflow inline in the
   ingress — the production path (step leases, run history, task usage,
   failure notify, workflow retry) via `worker.execute` with the worker's own
   attempt hooks — and answers 200 with the `template` rendered against
   `{trigger.*}` / `{steps.<id>.output.*}` (any JSON shape; default body
   `{"ok", "workflow", "steps", "event_id"}`), 202 `{"suspended": true,
   "resume_at": …}` when a delay parks the run (the continuation is enqueued
   exactly as the worker parks it), or 500 with the delivery's dedupe claim
   released (a provider retry re-executes) when the chain fails. Optional
   `status` (2xx, default 200) sets the success code and `budget_seconds`
   (1-25, default 10) the inline run's wall-clock budget. Dedupe composes: a
   retried delivery is answered, not re-run. Sync is a single-workflow
   contract — zero or several matching workflows fall back to the ack path
   (202, with a `reason`) — and the budget is enforced between steps, never
   mid-step: on overrun the un-run tail is enqueued under the same event id
   (the completed steps' leases dedupe the worker's replay) and the caller
   gets 202 `{"reason": "budget"}`, so the run still finishes out of band;
   longer chains belong in ack mode. Surfaces: the console hook dialog's
   Response select + template JSON field, `dapier hooks save` body files
   plus `--sync-response` (shown by `hooks show`). Tests:
   `tests/test_hook_sync_response.py`, `tests/test_sync_response.py`,
   `tests/test_webhook_sync.py`.
7. **CLOSED (2026-09-28)** — trigger dedupe: a TTL-bounded seen-id store
   (`triggers/seen.py`, one item per scope in the cursors table, 30-day
   expiry, 1000-entry cap, atomic `claim`/`forget`) closes both halves.
   `next_cursor` polls now skip items whose stable id was already published
   (providers recycle pages) and park the provider's continuation cursor —
   never an item id — once a page drained; watermark polls are unchanged
   (the cursor already dedupes). Webhook triggers take an optional
   `dedupe_path` (dot-path to a stable delivery id in the payload; console
   Triggers form and CLI `hooks save` both set it): a retried delivery
   claims the same event id, answers `202 duplicate`, and never publishes —
   a publish failure releases the claim so the retry still delivers, and a
   seen-store outage publishes anyway (the stable id still dedupes
   downstream through run grouping and step leases). Telegram triggers
   dedupe by default on `update_id`, the provider's own delivery identity.
   Tests: `tests/test_seen_store.py`, `tests/test_poll_dedupe.py`,
   `tests/test_poll_triggers.py`, `tests/test_hook_dedupe.py`.
8. **CLOSED (2026-09-28)** — tags: a workflow carries up to 20
   short labels in its YAML (`tags:`, validated by `_validate_tags`), set
   through `PUT /api/{admin,agent}/designer/workflows/<file>/tags`
   (audited `workflow.tags`, published with cause "tags", committed
   best-effort like the toggle), `dapier workflows tags <file> --tags
   a,b | --clear`, and a console Tags prompt per row. Tags are normalized
   to lowercase, deduped case-insensitively, and bounded (20 × 64 chars).
   Both list endpoints narrow with `?tag=` (case-insensitive) — the CLI
   mirrors it with `workflows list --tag`, and the console's Workflows
   view gets a tag dropdown plus tag chips per row. Tags travel with
   saves, deploys, duplicates, and rollbacks because they live in the
   definition. Bulk enable/disable is also in: audited
   `POST /api/{admin,agent}/designer/workflows/bulk`
   (`{"ids": [...], "action": "enable|disable"}` with per-id results,
   refusing parked-delay workflows like the single toggle) and
   `dapier workflows on|off <file>...` taking multiple files in one call.
   And folders close the slice: a flat `folder:` top-level YAML key
   (`_validate_folder` — ≤64 chars, never a path; Zapier folders are
   flat), `PUT /api/{admin,agent}/designer/workflows/<file>/folder`
   (audited `workflow.folder`, cause "folder"), `dapier workflows folder
   <file> --set "Name" | --clear` and `workflows list --folder`, `?folder=`
   on both list routes plus the overview payload (a `workflow_folders`
   aggregate beside the tags one), and a console Folder dropdown + folder
   chips + per-row Folder prompt. Tests: `tests/test_workflow_bulk.py`,
   `tests/test_workflow_folders.py`, `tests/test_workflow_tags.py`,
   `tests/test_workflow_delete.py`, `tests/test_workflow_delete_tags.py`.

### Round 5 (2026-09-28): fresh product-loop audit — coverage re-verified, polish landed

A read-only audit re-verified the connector coverage matrix against a live
`registry.catalog()` / `trigger_discovery_catalog()` import: every cell is
backed by a current registry entry, and the designer mirror's action/logic/
filter sets match the registry exactly (one drift caught mid-flight — the
mailchimp trigger chip landed on the Python side before the mirror; chips
always need the `make designer-console` rebuild). All four earlier
"Surprises" were re-checked and confirmed fixed with line evidence: dryrun
derives runners from the registry, a live step lease now requeues the
redelivery instead of skipping it (`LeaseBusy`), the `webhook` action returns
the response body, and poll failures notify the operator.

Polish findings from that audit, landed:

- **OAuth verify-failure refresh fallback** — `tokens.get_access_token` tries
  one `refresh_and_store` when a locally-fresh token fails provider
  verification (server-side revocation/rotation, clock drift), instead of
  wedging every workflow on the connection until natural expiry; the write is
  version-conditional and the replacement token is verified and bind-checked
  before storing. Tests: `tests/test_token_refresh_fallback.py`.
- **429/503 Retry-After under autoretry** — `webhook`/`http_request` failures
  raise a typed `HttpError` carrying `status` (plus a parsed seconds
  `retry_after` where the response headers are readable); an opted-in
  `autoretry` sleeps `min(max_seconds, retry_after)` plus jitter on 429/503
  instead of blind backoff. Steps without `autoretry` are unchanged, and
  provider 4xx/5xx on the default-urllib paths now surface as `HttpError`
  rather than a raw `urllib.error.HTTPError`. Tests: `tests/test_retry_after.py`.
- **`list_versions` scan capped** at `SCAN_LIMIT` like its sibling
  `load_items` (was an unbounded full-table scan on every versions read and
  rollback). Tests: `tests/test_published_workflows.py`.
- **Overview executions block ordered by `started_at`** — the old sort was
  reverse string order on `{workflow}:{action}:{event}` ids, which is not
  chronological, so the block could show stale rows beside the correctly
  ordered runs list. Tests: `tests/test_admin.py`.
- **Error summary/digest honesty about the 200-run cap** — the summary
  payload carries `bounded`/`cap` when the failed-run scan hit the limit and
  the daily digest email renders a "most recent N failed runs are counted"
  caveat, so a >200-failure day no longer reads as exactly 200. Tests:
  `tests/test_error_digest.py`.

Still open (ranked, from the same audit):

1. **CLOSED (2026-09-28)** — Mailchimp webhook lifecycle: a mailchimp hook
   trigger now binds its audience (`list_id`, plus optional subscribed
   `events` and a mailchimp `connection_id`) and drives
   Mailchimp's `POST/DELETE /lists/{id}/webhooks` server-side — save
   subscribes the hook URL (replacing any webhook already registered under
   it) with every source subscribed, and delete/disable unsubscribes by
   listing the audience's webhooks and deleting ours by id. Best-effort on
   both ends: a Mailchimp failure never blocks the stored change, it comes
   back as `warnings` on the response (surfaced by `dapier hooks
   save/delete`); the shared `mailchimp` credential or the bound
   connection's key authenticates, datacenter derived from the key suffix.
   Deliveries match for real: the intake publishes the URL's hook name in
   the event data, a trigger's stored workflow fans out to one trigger spec
   per subscribed type (matching is exact on event, so the old single
   `message.received` spec never fired), and the trigger view/CLI carry no
   bearer hint — Mailchimp sends no auth headers.
   Tests: `tests/test_mailchimp_webhook_lifecycle.py`,
   `tests/test_hook_triggers.py`, `tests/test_mailchimp_intake.py`.
2. **CLOSED (2026-09-28)** — formatter edges: `date_format` takes an
   optional IANA zone as a `@`-suffix on its raw argument
   (`{v|date_format:%Y-%m-%d %H:%M@Europe/Berlin}` — zoneinfo conversion,
   offset-less values read as UTC, unknown zone renders empty per the
   never-raise rule); new zero-arg `time_until` relative formatter
   (`in 3h`, `5m ago`, `just now` within a minute; parses via
   `logic.parse_moment`); `number_format:currency:EUR[:decimals]`
   (€/$/£ table, unknown codes fall back to `1,234.56 JPY` shape),
   validated mode-aware at save time. Tests: `tests/test_templating.py`
   FormatterEdgeTests.
3. **CLOSED (2026-09-28)** — all-workflows export bundle:
   `dapier workflows export --all [-o out.zip]` lists via the agent
   designer-list endpoint, fetches each workflow's canonical YAML through
   the same single-export endpoint, and zips them as flat `<file>.yaml`
   (empty list refuses politely rc 2; a workflow without YAML is skipped
   with rc 5 and the rest bundled). Console parity holds through the
   existing per-workflow Download YAML — both drive the same API. Tests:
   `tests/test_cli.py`.

4. **CLOSED (2026-09-28)** — run-history CSV export, three surfaces over
   one domain function (`runs.api_export`, modeled on the audit trail's
   export): the list's filters (`workflow`, `status` incl. the
   success/problems aliases, `since`/`before`, content search `q`),
   newest first, capped at `max_rows` (default 500, max 2000,
   `truncated` flags the clip), returning
   `{filename, count, truncated, csv}` with one row per run
   (`run_id … error`, booleans as true/false). Audited like every bulk
   read: `runs.export` lands in the operator trail. Surfaces: audited
   `GET /api/admin/runs/export`, operator-gated
   `GET /api/agent/runs/export`, `dapier runs export
   [--workflow|--status|--since|--before|--search|--max-rows|--out]`
   (writes the server's suggested filename by default), and the console
   Runs view's Export CSV button (downloads the filtered history at the
   2000-row cap). Tests: `tests/test_runs.py`, `tests/test_admin.py`,
   `tests/test_agent_api.py`, `tests/test_cli.py`.

5. **CLOSED (2026-09-28)** — YouTube subscribe-on-save: saving, toggling,
   renaming, or rolling back a workflow whose trigger is `youtube /
   video.published` syncs the WebSub hub immediately
   (`youtube_subscriptions.reconcile`, called best-effort from the designer
   store): newly watched channels are subscribed on save, and channels the
   change orphaned are unsubscribed — but only when no other enabled
   workflow still names the channel, and only the delta, so an edit that
   leaves the channel list alone never rings the hub. Disable and delete
   unsubscribe the same way; every hub failure comes back as `warnings` on
   the save/toggle/bulk/delete response (surfaced by `dapier workflows
   save/on/off/delete`) instead of blocking the change, and the 5-day
   renewal schedule remains the reconciliation pass for anything missed.
   Deploy note: the API function now also needs `YOUTUBE_CALLBACK_URL` in
   template.yaml. Tests: `tests/test_youtube_on_save.py`,
   `tests/test_youtube_subscriptions.py`.
6. **CLOSED (2026-09-28)** — Dropbox and Slack action breadth (Zapier's
   file-management and channel-management sets). Dropbox gains
   `dropbox_create_folder` (files/create_folder_v2; an existing folder
   errors — `dropbox_find` with `create_if_missing` stays the find-or-create
   path), `dropbox_move` (files/move_v2 — also Zapier's Rename File: a move
   within the same folder under a new name), and `dropbox_copy`
   (files/copy_v2); both transfer actions render both paths from the event
   and take `autorename` to suffix on conflict, outputting
   `{moved|copied, item}`. Slack gains `slack_dm`
   (conversations.open + chat.postMessage — chain `slack_find_user` and pass
   `{steps.<id>.output.user.id}`), `slack_create_channel`
   (conversations.create; the name is normalized to what Slack accepts —
   lowercase, spaces to hyphens, illegal characters dropped, ≤80 chars — so
   templated names never fail on a stray character), `slack_set_topic` and
   `slack_set_purpose` (conversations.setTopic/setPurpose); alongside the
   update-message and add-reaction actions these complete Zapier's core
   Slack set. All ride the registry (designer mirror + console bundle
   rebuilt) and the existing run/Test-step surfaces — no new endpoints.
   Tests: `tests/test_dropbox_slack_actions.py`.
7. **CLOSED (2026-09-28)** — Slack trigger variety: the Events intake no
   longer folds everything into `message.received`. `slack_events.event_name`
   maps each subscribed Slack type to its own workflow-facing event —
   `message.*` keeps `message.received`, `app_mention` publishes
   `app.mention`, `reaction_added` publishes `reaction.added`,
   `member_joined_channel` publishes `member.joined` (anything else still
   answers `accepted: false`; bot posts still never publish) — and the
   envelope flattens `reaction`, `item_ts` and `inviter` beside the message
   fields. The Slack chip declares all four events, the sample pull serves
   one documented delivery per event (`message.received` stays the live
   channels walk; a reaction or join never shows in history, so the others
   fall back to recorded runs then the documented delivery), and the
   designer mirror's connector catalog carries the same four. Compatibility
   note: `app_mention` no longer also fires `message.received` — workflows
   that relied on that re-filter on `app.mention`
   (docs/connectors/slack.md documents the setup and the mapping).
   Tests: `tests/test_slack_event_variety.py`, `tests/test_slack_events.py`
   (per-event sample pull), `tests/test_slack_actions.py`.
8. **CLOSED (2026-09-28)** — Mailchimp and Google Drive parity round: the
   audience and the drive gain their missing Zapier staples on all
   surfaces. Mailchimp gains `mailchimp_remove_member` (DELETE on the
   email's md5 path; a missing member is `{removed: false}`, not an
   error) and `mailchimp_tag_member` (POST `members/{md5}/tags` — Add
   applies the tag, Remove sets it inactive), and its first poll source:
   `mailchimp.members` lists one audience through the Marketing API with
   the stored key (a named `connection_id` or the shared `mailchimp`
   credential — no OAuth required) and publishes `mailchimp`/`member.new`
   on a `last_changed` watermark — seeded first fire (the roster is
   history, not news), oldest-first emission, and the seen-set keeping a
   profile edit from re-firing an already-seen member. Google Drive gains
   the change-feed sources Zapier's Updated/Deleted File triggers need —
   `google-drive.updates` (`file.updated`, folder-scoped when `folder_id`
   is stored) and `google-drive.deletions` (`file.deleted`, always
   Drive-wide: a removal change carries a fileId only) — sharing one
   `changes.list` fetch whose opaque page-token cursor parks only once a
   page drains, so the budget and seen-set machinery work unchanged; plus
   `drive_share_file` (permissions.create — user/group/domain/email
   grantee) and `drive_copy_file` (files.copy). The chips declare
   `member.new` / `file.updated` / `file.deleted`, the sample pulls serve
   each (stored-poll live pulls first: Drive's changes sources sample
   against the parked cursor read-only, Mailchimp's against the epoch
   watermark; fallbacks key on the poll's event), and the designer mirror
   carries the actions and events (console bundle rebuilt). Tests:
   `tests/test_round_mailchimp_drive.py`, `tests/test_mailchimp_actions.py`,
   `tests/test_mailchimp_poll.py`, `tests/test_drive_changes.py`.
9. **CLOSED (2026-09-28)** — Slack gets its no-app poll source:
   `slack.messages` lists one channel through `conversations.history` (two
   pages of 200, watermark = the newest ts) with the stored bot token and
   publishes the same `message.received` event the Events intake does —
   scheduled polling feeds the identical pipeline with no Slack app and no
   Event Subscriptions setup; the first fire seeds without emitting, and
   bot posts and membership subtypes never flatten (the intake's rule).
   The chip needs nothing new; the sample pull walks the channel live.
   Tests: `tests/test_round_slack.py`.
10. **CLOSED (2026-09-28)** — Sheets and Telegram action staples:
   `sheets_delete_row` (worksheet title → sheetId, then batchUpdate
   deleteDimension; pairs with sheets_lookup_row's row output) and
   `sheets_create_spreadsheet` (spreadsheets.create, optional header row
   written through the existing values.update path — the output
   spreadsheet_id/url feed a follow-up append); `telegram_send_photo` and
   `telegram_send_document` (Bot API multipart, chat_id falling back to
   the triggering chat; media comes from source_url — dapier fetches it
   itself, so Telegram need not reach it — or a staged source_s3
   {bucket, key}). Tests: `tests/test_round_sheetstelegram.py`.
11. **CLOSED (2026-09-28)** — YouTube, S3 and Zoom action staples plus
   http_request response visibility: `youtube_add_to_playlist`
   (playlistItems.insert — directly serves the DTC channel workflow) and
   `youtube_update_video` (videos.update part=snippet; title required —
   YouTube replaces the snippet whole, and omitting category clears it);
   `s3_read_object` (GetObject staged like the dropbox read),
   `s3_presign_url` (presigned GET, one hour by default up to SigV4's
   seven-day ceiling — the dropbox get_temp_link symmetry) and
   `s3_delete_object` (idempotent DeleteObject); `zoom_update_meeting`
   (PATCH /meetings/{id} with only the filled fields — an empty update is
   rejected) and `zoom_add_registrant` (POST registrants; join_url is
   unique per registrant). `http_request` and the webhook action surface
   `response_headers` (lowercased) whenever the transport surfaced them —
   legacy two-key outputs unchanged. Tests: `tests/test_round_yts3zoom.py`,
   `tests/test_http_response_headers.py`.
12. **CLOSED (2026-09-28)** — mailchimp.members watermark hardened: the
   cursor is now the composite `last_changed|id` (the zoom and s3 sources'
   pattern), so members changed in the same second as the last fired
   member are no longer skipped until something else bumps the watermark.
   Tests: `tests/test_round_mailchimp_drive.py` (same-second regression
   pin).
13. **CLOSED (2026-09-28)** — Multi-user roles v1: authorization grows from
   a single operator allowlist plus per-connection grants to a named user
   role model. A DynamoDB store (`RoleAssignmentsTable`, one row per DTC
   subject or email, in `src/dapier/auth/roles.py`) carries a role —
   `viewer` (read-only console: overview, runs, audit, designer reads,
   discovery, trigger samples), `editor` (also workflow editing/testing,
   replays and cancels, storage writes), `operator` (the historical
   capability set: also connections, credentials, grants, tokens, trigger
   definitions), `admin` (also user management) — plus an optional display
   name and a `disabled` flag that denies an account everywhere. Resolution:
   a stored row wins, narrowing an allowlisted operator to viewer or
   widening a non-allowlisted identity into a band; with no stored row the
   `OPERATOR_EMAILS`/`OPERATOR_SUBJECTS` allowlist stays authoritative —
   answering `admin` while the store is empty, so today's operators keep
   full power and can bootstrap the first admin, and `operator` once any row
   exists — which makes an empty table reproduce the pre-roles gate bit for
   bit, and store failures degrade to the allowlist verdict rather than
   failing a request. The last active admin cannot be demoted, disabled, or
   removed (the API refuses with 409). Enforcement is centralized in the
   auth layer: the console dispatcher gates every route through
   `session.require_role(event, roles.minimum_for_route(method, path))`, the
   agent API through `require_operator`'s `minimum_for_action` band, both
   sharing `roles.satisfies(roles.effective_role(payload))`. Surfaces
   (parity): console `GET/POST /api/admin/users` plus
   `DELETE /api/admin/users/{subject}` behind a Users view (list, set-role
   dialog with display name and disabled flag, remove with confirm);
   `dapier users list|set-role|remove` over the operator- and admin-gated
   `/api/agent/users`; every mutation audited as `users.set-role` /
   `users.remove`. Tests: `tests/test_roles.py` (domain semantics, dispatcher
   bands, bootstrap-then-narrow transitions, last-admin guard, disabled and
   API-token denials, agent users API), `tests/test_users_cli.py` (command
   surface, confirmations, exit codes). Shared workspaces and invitations
   remain future work.
14. **CLOSED (2026-09-28)** — find-or-create breadth and the mailchimp
   unsubscribe staple. `mailchimp_unsubscribe_member` PATCHes the member's
   status to unsubscribed — the reversible counterpart of the permanent
   remove (upsert's `status_if_new` only applies to members the audience
   does not have yet, so it could not unsubscribe an existing one); a
   missing member is `{unsubscribed: false}`, not an error.
   `create_if_missing` now covers every find with a meaningful create:
   `mailchimp_find_member` upserts on a miss (`status` applies as
   status-if-new, optional merge fields, `created: true` in the output —
   Zapier's Find or Create Member), and `slack_find`'s channel branch
   creates a missed name via `conversations.create` (`is_private` honored;
   Zapier's Find or Create Channel — a user miss stays a miss), joining
   the existing sheets/dropbox/zoom finds. Engine runners, registry
   entries and designer catalog mirrors; tests:
   `tests/test_mailchimp_actions.py`, `tests/test_find_slack_dropbox.py`.

14. **CLOSED (2026-09-28)** — Workflow templates v1: a workflow flagged
   `template: true` (the flag rides the workflow YAML, set through
   `api_template_flag`) joins the gallery that `api_templates` serves —
   the bundled starters in `workflows/template-*.yaml` plus anything an
   operator published, the published overlay winning per id. Forking is
   `api_apply_template`: the flagged workflow loads under a new
   `<template-id>-copy` id and goes through the same commit-and-publish
   path as a save. All three surfaces: the designer's "Start from a
   template" gallery (console bundle rebuilt), `GET/POST
   /api/admin/designer/templates*`, the operator-gated `/api/agent/designer/templates*`
   trio, and `dapier templates list|apply|publish|unpublish`. Tests:
   `tests/test_designer.py` (store + admin routes), `tests/test_cli.py`
   (command surface). Cross-account transfer still needs the multi-user
   model (item 13); marketplace-style sharing beyond this deployment
   remains future work.
15. **CLOSED (2026-09-28)** — Designer editor polish: undo/redo over a
   per-session draft timeline (`designer/src/history.ts`, pure module —
   500ms sliding coalescing per editing group, 50-entry cap, reset on
   open and after a successful save; Ctrl/Cmd+Z, Ctrl/Cmd+Shift+Z /
   Ctrl+Y, toolbar buttons), step duplication (fresh id, `-copy`/`(copy)`
   suffixes, inserted after the original), cross-workflow step copy/paste
   (localStorage clipboard, pasted steps validate like manual ones), and
   a "?" shortcuts overlay. Editor-session state only — no API surface,
   so under the parity rule's presentational exception.
16. **CLOSED (2026-09-28)** — Zoom round completion, behind item 11:
   `zoom_delete_meeting` (DELETE /meetings/{id}, optional occurrence_id
   for recurring scoping, output `{deleted, meeting_id}`) and
   find-or-create on `zoom_find_meeting` (`create_if_missing` reuses the
   create path's payload rules; a hit or a meeting-id lookup never
   creates; output gains `created`). Find-or-create verdicts elsewhere:
   `slack_find_user` (no user-create API), `drive_find_file` (a miss
   composes with drive_upload_file), `s3_find` (an empty object is
   storage noise) — deliberately skipped, documented in
   `docs/connector-coverage-audit.md`. Tests: `tests/test_zoom_delete_meeting.py`,
   `tests/test_slack_drive_media.py`, `tests/test_find_media.py`.
17. **CLOSED (2026-09-28)** — the last Zapier Slack action staple:
    `slack_add_reminder` (reminders.add — Zapier's Add Reminder). `text`
    renders from the event; `time` takes Slack's natural-language times
    (`in 20 minutes`, `tomorrow 9am`) or epoch seconds (a bare number is
    sent as epoch, so a rendered timestamp reads as a moment, not a
    phrase); empty = Slack's default (20 minutes). The reminder belongs
    to the token's own user — reminders.add cannot target someone else.
    Output `{ok: true, reminder: {id, time, text}}`. Engine runner,
    registry entry, designer mirror. Tests: `tests/test_slack_reminder.py`.
    With this, every Slack action in Zapier's catalog has a counterpart
    (send/thread/DM/schedule/update/react/pin/invite/create-channel/
    topic/purpose/upload/find-channel/find-user/find-message/reminder),
    and Telegram's catalog is fully covered too (send/photo/document/poll;
    find chat goes beyond it). The stale Telegram matrix rows (still
    listing only `message.received` + find chat) are corrected.
