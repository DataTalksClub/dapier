---
name: create-flows
description: Author, validate, test, and publish Dapier flows (workflow YAML) through the CLI/API. Covers the flow schema, trigger connectors and filter operators, the action catalog, templating, connections, and the draft→test→publish lifecycle. Use when the user asks to create, change, fix, or review a dapier flow, workflow, automation, trigger, or zap.
---

# Create Dapier flows

A flow is one workflow YAML document: a trigger plus a chain of actions.
It lives in the managed workflows store. Saves create drafts; publishing
promotes a definition live, with optional Git sync. The engine runs the
published definition once enabled. Author the YAML
and drive everything through the `dapier` CLI or the HTTP API — the console
designer is a convenience layer over the same API, never a separate path.

Do not edit the workflows store by hand and do not invent YAML shapes: the
schema below plus the sources of truth are the contract.

## Sources of truth — never invent

- **Action types and their fields**: `designer/src/catalog.ts` → `actionCatalog`
  (mirrors the engine's `run_*` dispatch in `src/dapier/engine/`).
- **Trigger connectors and events**: `connectorCatalog` in the same file.
- **Filter/logic operators**: `filterOperators` / `logicOperators` there too.
- **What a save rejects**: `src/dapier/api/designer_store/validation.py` → `parse_workflow`.
- **Runtime semantics** (templating, error policies, sandboxed code):
  `src/dapier/engine/` (`logic.py`, `matching.py`, `actions/`).
- **Provider setup, connection recipes, scope lists**: `docs/connectors/*.md`.

Save validation is deliberately permissive about action internals — an unknown
`type` round-trips at save and then fails at run time. Always cross-check every
action type and field against `catalog.ts` before saving.

## Golden path

```bash
dapier connections list                                  # 1. connections exist?
dapier workflows sample email                            # 2. realistic event shape
dapier workflows test flow.yaml --event @event.json --strict   # 3. dry-run
dapier workflows save flow.yaml                          # 4. validates + saves a DRAFT (nothing live)
dapier workflows draft-diff flow.yaml                    # 5. review draft vs live
dapier workflows publish flow.yaml                       # 6. promote draft live (v1 for a new flow)
dapier workflows on flow.yaml                            # 7. enabled → it runs
dapier runs list --workflow invoice-intake               # 8. verify real runs
```

- `save` accepts a file path or `-` (stdin). It runs `parse_workflow`; fix
  every reported error before anything else.
- `test` is a dry-run by default; `--execute` performs real side effects
  (use sparingly, deliberately). `test-step --action <id>` exercises one step
  with `--steps '{"<id>": {"status": "...", "output": ...}}'` feeding
  `{steps.*}` templates.
- `publish` promotes the draft (409 when the draft is stale behind live);
  `discard --yes` throws a draft away; `versions` / `diff` / `rollback`
  manage history; `on`/`off` (aliases `enable`/`disable`) flip the live
  switch, bulk via `--tag`/`--all`.

A copilot NL→YAML generator exists (`dapier workflows draft "..."`) but author
the YAML directly — you have the full schema, the model does not.

## Schema

```yaml
id: invoice-intake            # required; kebab-case/digits/underscores, max 63
enabled: true                 # default true
description: File emailed invoices
tags: [billing, ops]          # optional; used by --tag filters
folder: Billing               # optional; flat, one folder max
notify: [ops@example.com]     # optional; one SES email per failed run
auto_pause_after: 5           # optional; pause after N consecutive failed runs (false = off)
trigger:                      # exactly one of trigger / triggers
  connector: email
  event: message.received
  filters:                    # optional; see Filter rules
    subject:
      contains: invoice
actions:                      # non-empty, run in order
  - id: save-pdf              # unique within the workflow
    type: dropbox_upload
    connection_id: dropbox Alexey Grigorev   # "<service> <account>"
    folder: /invoices
  - id: notify
    type: slack
    connection_id: slack           # a bare service when only one account has it
    channel: "#billing"
    text: "Filed {subject} → {steps.save-pdf.output.path}"
```

- `connection_id` names a connection as `<service> <account>`
  (`drive alexey@datatalks.club`, or any unique part of the account:
  `drive datatalks`), the same reference the CLI takes. A bare service works
  when only one account has it. The engine resolves it at run time; an
  ambiguous or unknown reference fails the step with the candidates. Legacy
  internal ids (`google-sheets`) still work, but write references in new
  flows — `dapier connections list` shows the accounts.
- `triggers:` is a list of trigger mappings that share the same actions
  (multi-trigger flow). Every trigger needs `connector` and `event`.
- Email triggers and their actions live only in workflow definitions.
  `dapier emails list|show` is an inventory, not another flow editor.
  `dapier emails migrate <name>` converts a remaining legacy record unchanged.
- Legacy shared `flow:` references are retired. Put actions in the workflow;
  reuse another managed workflow with `run_workflow`.
- Publishing refuses overlapping email routes with 409. To deliberately fan
  out to several workflows, set `allow_email_overlap: true` explicitly; the
  designer YAML view supports this same workflow setting.
- Write `actions` before `trigger` (house style: `id`, `enabled`, then
  actions, then trigger last).

### Trigger connectors and events

`email` (message.received, bounce.received, complaint.received — filter on
`route`, `sender`, `subject`), `webhook` (request.received), `telegram`
(message.received, channel_post.received, callback_query.received), `slack`
(message.received, app.mention, reaction.added, member.joined), `dropbox` /
`google-drive` / `s3` (file.created, file.updated, file.deleted), `youtube`
(video.published), `zoom` (recording.completed, meeting.started, …),
`mailchimp` (subscribe, campaign, member.new, …), `google-sheets` (row.new,
row.updated), `google-calendar` (event.new), `gmail` (message.received),
`rss` (item.new), `renderer` (job.completed), `schedule`
(schedule.triggered), `poll` (item.new), `custom`.

- Filterable fields per connector: pull a real sample —
  `dapier workflows sample <connector>` prints the `{trigger.*}` fields.
- `schedule` and `poll` triggers are stored trigger records managed with
  `dapier schedules` and `dapier polls`; the workflow filters on their id,
  e.g. `filters: {schedule: {equals: morning-digest}}`.

### Filter rules

`filters` maps an event field to **exactly one** rule:

```yaml
filters:
  path: {prefix: /incoming/, suffix: .pdf}
  sender: {in: [billing@acme.com, ops@acme.com]}
```

Operators: `equals`, `not_equals`, `in`, `prefix`, `suffix`, `contains`,
`does_not_contain`, `gt`, `gte`, `lt`, `lte` (numeric when both sides parse
as numbers, else lexicographic — ISO dates order correctly), `exists`, `empty`.
The same operators drive the in-flow `filter`/`condition` steps.

## Action catalog

Every action: unique `id` + `type`. Steps run in order; each step's output is
captured for later `{steps.<id>.*}` templates. Full field lists, defaults,
required flags, and which fields need which provider:
`designer/src/catalog.ts`. The families:

- **Core**: `agent` (prompt, workspace, engine), `webhook` (url, secret_id,
  timeout_seconds), `http_request` (url, method, headers, body, auth_*,
  connection_id), `ai_complete` (prompt, system, json_mode, model), `dataops`
  (intake push), `render_html_to_pdf`, `run_workflow` (sub-workflow),
  `email_send` (to, subject, text/html, cc/bcc, attachments — SES, no
  connection).
- **Slack**: `slack`, `slack_dm`, `slack_find`, `slack_find_user`,
  `slack_find_message`, `slack_update_message`, `slack_schedule_message`,
  `slack_add_reminder`, `slack_add_reaction`, `slack_create_channel`,
  `slack_set_topic`, `slack_set_purpose`, `slack_invite_to_channel`,
  `slack_pin_message`, `slack_upload_file`.
- **Telegram**: `telegram_send`, `telegram_find_chat`,
  `telegram_send_document`, `telegram_send_photo`, `telegram_send_poll`,
  `telegram_edit_message`, `telegram_pin_message`, `telegram_ban_member`,
  `telegram_unban_member`.
- **Files**: `dropbox_upload|delete|find|read_file|get_temp_link|
  create_folder|move|copy`; `s3_upload|find|list_objects|read_object|
  presign_url|delete_object` (uses `credential_id`); `drive_find_file|
  read_file|upload_file|copy_file|share_file|delete_file|move_file|
  create_folder`.
- **Google**: `calendar_create_event|quick_add|find_events|update_event|
  delete_event`; `sheets_append_row|find_row|lookup_row|update_row|
  delete_row|clear_values|create_spreadsheet|add_worksheet|create_column`.
- **Marketing/CRM**: `mailchimp_find_member|upsert_member|remove_member|
  unsubscribe_member|tag_member` (uses `credential_id`).
- **Zoom**: `zoom_find|create|update|delete_meeting`, `zoom_find_recording`,
  `zoom_delete_recording`, `zoom_find|create|update|delete_webinar`,
  `zoom_add_registrant`, `zoom_add_webinar_registrant`,
  `zoom_list_past_participants`, `zoom_list_past_webinar_participants`.
- **Logic steps** (no connection): `filter` (field/operator/value — stops the
  run when it does not match), `condition` (then/else step lists), `paths`
  (branches), `delay` (seconds/minutes/hours/days/until), `for_each`
  (list, item, actions, max_iterations), `digest` / `digest_add` /
  `digest_flush` (accumulate then release), `csv_parse`, `csv_format`,
  `storage_get|set|delete|find` (key/value state), `code` (sandboxed Python:
  stdlib allow-list, no network/fs), `js` (JavaScript in V8 via mini-racer —
  no fs/network/process). Both code steps take `code` + `timeout_seconds`
  (0.5–15, default 5) and produce `{result, stdout}`.

### Templating

`{field}` pulls event data; the whole event is mirrored under `trigger.*`;
earlier steps under `steps.<id>.output.*`, plus `steps.<id>.status` and
`steps.<id>.error`. Pipes transform: `{trigger.subject | trim | upper}`,
`{steps.find.output.rows | sum:amount}`.

### Per-step failure policy

- `on_error: halt|continue|run` (default halt) + `error_actions:` sub-chain
  (`{steps.<id>.error}` is templatable there).
- `on_fail: continue|halt` — the lighter policy (continue absorbs the failure;
  the step reads `skipped` in history). Mutually exclusive with `on_error`
  on the same step.
- `autoretry:` mapping (connector actions only): `attempts` 1–3,
  `initial_seconds`/`max_seconds` 1–60, exponential backoff + jitter; the
  step output records `attempts`. Validated at save by
  `registry.validate_autoretry_key`.

## Connections

Every `connection_id` must name an existing connection
(`dapier connections list`); the engine resolves and refreshes its token at
run time and checks grants. A connection that exists but lacks user consent is
not usable — `dapier connections connect <id>` (OAuth) or `dapier connections
import` (token) first. `s3`/`aws` and `mailchimp` are pseudo connections
backed by stored API keys referenced as `credential_id`. Scope changes go
through `dapier connections scopes`; provider setup lives in
`docs/connectors/*.md`.

## Rules

1. Nothing goes live without an explicit `publish` + `enabled` — a save is
   always a draft.
2. Never reference a connector/event/action type not present in the catalog.
3. Test before publishing: dry-run with `--strict` against a sample from
   `dapier workflows sample`; reserve `--execute` for deliberate live checks.
4. One flow, one purpose: prefer a new flow over piling branches into an
   existing one; reuse via `run_workflow`.
5. The console designer drives the same API (admin routes vs the CLI's agent
   routes) — parity is repo law (AGENTS.md). A flow change that touches code
   ships its API/CLI/console pieces and tests together.
