# Connector coverage matrix — discovery / replay / test

Audit of the Zapier-parity surfaces (browse live resources, replay past
events, test credentials) across every connector, dumped live from the
registry (`registry.ACTIONS / DISCOVERIES / CONNECTION_TESTS`,
`trigger_discovery.TRIGGER_DISCOVERIES`). Regenerate the raw dump with
`.tmp/coverage-audit/dump_registry.py` → `.tmp/coverage-audit/registry_dump.json`.

## Providers (connection-capable): 7/7 tested, 7/7 discoverable

| Provider | ConnectionTest | Discovery sources | Notes |
|---|---|---|---|
| google | ✅ (`sheets.py`, key `google`) | google-sheets, google-drive | one OAuth connection serves both |
| youtube | ✅ (`youtube.py`) | youtube | |
| zoom | ✅ (`zoom.py`) | zoom | |
| dropbox | ✅ (`dropbox.py`) | dropbox | |
| slack | ✅ (`slack.py`) | slack | token provider |
| telegram | ✅ (`telegram.py`) | telegram | token provider |
| aws | ✅ (`s3.py`, key `aws`, alias `s3`→`aws`) | s3 | credential, not a connection record — pseudo-connection |

`PROVIDER_DISCOVERY_SOURCES` covers every provider; every discovery
connector resolves from at least one provider key. Test endpoint
`POST /api/agent|admin/connections/{id}/test`, CLI `dapier connections test`,
console Credentials "test" (`views/connections.js`). Discovery endpoints
`GET …/connections/{id}/discover[/{resource}]` on agent + admin, CLI
`dapier connections discover`.

## Discovery resources: 20 registered

| Resource | Consumed by action field hints |
|---|---|
| slack.channels | `slack.channel` |
| telegram.chats | `telegram_send.chat_id`, `telegram_find_chat.chat_id` |
| dropbox.folders | `dropbox_upload.folder`, `dropbox_find.path` |
| dropbox.files | `dropbox_delete.path` |
| dropbox.search | `dropbox_find.query` |
| google-drive.files | `drive_find_file.name`, `s3_upload.source_url` |
| google-drive.folders | `drive_find_file.folder` |
| google-sheets.spreadsheets / worksheets / columns | all four `sheets_*` actions (spreadsheet → worksheet → column cascades) |
| google-sheets.rows | `sheets_update_row.row` |
| s3.buckets / s3.objects | `s3_upload`, `s3_find` (bucket → prefix/key cascade; objects takes an optional `prefix` param) |
| zoom.meetings | `zoom_find_meeting.meeting_id` |
| zoom.recordings | `zoom_find_recording.meeting_id` (also the action's data source) |
| slack.users, slack.messages | `slack_find_user.email` (slack.users picker); slack.messages **browse-only** |
| youtube.channel | **browse-only** (static "connected channel" fact, nothing to select) |
| youtube.playlists | `youtube_find_playlist_items.playlist_id` |
| youtube.playlist_items | `youtube_find_playlist_items` (the action's data source) |

Browse-only resources still work end-to-end (CLI/console list + items); they
lack a designer field that would use them. Remaining: `youtube.channel` (a
static fact about the connection, not a selectable list) and `slack.messages`
(a candidate for future message-template actions).

## Actions: 32 registered, hint coverage

Every action with a connection-bound *selection* field carries a `discover`
hint. The only ones without (and why that's acceptable):

- `slack_find.query`, `slack_find.find` — free-text search term, nothing to list.
- `youtube_find_video.query` — free-text search.
- `http_request`, `webhook`, `email_send`, `code`, `js`, `render_html_to_pdf`,
  `dataops` — no provider-backed selection fields (URLs, buckets-by-env, free text).
- `storage_get|set|delete|find`, `run_workflow` — G12/G13 additions: free keys
  and workflow ids, nothing provider-backed to list.

Designer catalog mirror (`designer/src/catalog.ts`) is in sync with the
registry — `tests/test_designer_pickers.py` green; bundle
(`src/web/designer.js`) rebuilt after the last catalog edit.

## Triggers: 9/9 palette connectors have sample discovery

custom, dropbox, email, poll, renderer, schedule, slack, youtube, zoom — all
register `TriggerDiscovery(kind="sample")`, live where the provider allows
(slack: the newest channel message, delivery-shaped via
`triggers.intake.slack_events.event_data`), else history-first with
synthetic fallback — and multi-event connectors (dropbox, zoom) serve one
documented payload per declared event. Field options (`kind="options"`):
dropbox folders, s3 buckets, s3 objects (bucket passed as `event`), slack
channels, telegram chats, zoom meetings, google-sheets spreadsheets,
google-drive files. Extras beyond the palette: dataops, webhook — used by
trigger setup flows.

## Replay & test surfaces

| Capability | API | CLI | Console |
|---|---|---|---|
| Run replay | ✅ agent + admin | ✅ `dapier runs replay` | ✅ runs view |
| Inbox replay | ✅ agent + admin | ✅ `dapier inbox replay` | ✅ Trigger inbox view |
| Workflow test (dry-run) | ✅ agent `designer/workflows/test` (+ per-file) | ✅ `dapier workflows test` | ✅ designer Test |
| Workflow step test (per-step, live) | ✅ agent `designer/workflows/test-step` (+ per-file) | ✅ `dapier workflows test-step` | ✅ designer inspector "Test step" |
| Connection test | ✅ agent + admin | ✅ | ✅ Credentials view |
| Trigger sample (`/discover` POST) | ✅ agent + admin | ✅ `dapier triggers` | ✅ designer |

## Gaps found

1. ~~Console inbox replay~~ — closed: the Trigger inbox view
   (`src/web/js/views/inbox.js`) lists events, opens the stored envelope and
   replays with a confirm dialog.
2. ~~Browse-only discoveries~~ — closed for the six actionable resources
   (round 5, below); `youtube.channel` and `slack.messages` stay browse-only
   by design (static connection fact / no matching action yet).
3. Both previously failing tests are fixed upstream
   (`test_discovery.py` cursor test passes; worker lease test renamed to
   `test_is_pending_raises_leasebusy_while_lease_is_live`, passes) —
   full `make test` re-run still gates the merge.

## Round 4, 2026-09-28: surfaces closed

- Trigger management is on all three surfaces: `dapier
  hooks|schedules|polls list|save|delete`, console `/triggers` (hooks +
  polls) and `/schedules`, over the existing `/api/{admin,agent}/*-triggers`
  routes.
- Fresh provider token on the console: Connections-view "Get token" over
  `POST /api/admin/connections/{id}/token` (same domain call as
  `dapier token exec|write`).
- Designer: Copilot draft dialog (`/api/admin/copilot/draft`) and the
  "Insert from previous steps" output picker (`{steps.<id>.output.*}` chips).
- Actions: `http_request` auth variety (`auth_type: basic|bearer|api_key`),
  `webhook` optional templated `payload`.
- Remaining known gap: per-connector trigger *variety* (more event types per
  provider) — design work, not surface parity.

## Round 5, 2026-09-28: browse-only discoveries wired to actions

- `zoom_find_recording` — cloud recordings by meeting id (404 is a
  `found: False` verdict), by topic over the 30-day listing, or the latest
  with no arguments; `zoom.recordings` feeds its `meeting_id` picker.
- `youtube_find_playlist_items` — a playlist's videos newest-first, same
  `{found, count, videos, video}` shape as `youtube_find_video`; dead
  playlist id is a verdict. `youtube.playlists` feeds its `playlist_id`
  picker, `youtube.playlist_items` is its data source.
- `drive_find_file` gains an optional templated `folder`
  (`'…' in parents` clause); `google-drive.folders` feeds the picker.
- Remaining known gaps: trigger variety (unchanged), plus `slack.messages`
  browse-only until a message action wants it.

## Round 6, 2026-09-28: slack Events trigger — the last connector gets a trigger half

- `POST /hooks/slack/{connection_id}` (`triggers.intake.slack_events`,
  zoom-style): per-connection Request URL, `X-Slack-Signature` verification
  against the app signing secret stored on the connection credential
  (`importing.token_secret_value`, shared by console and CLI save paths),
  `url_verification` handshake, deterministic event ids for Slack retries.
- Publishes `slack / message.received` for the message family
  (`message.*`, `app_mention`); app posts (`bot_id`) never publish, so a
  workflow posting into the channel it listens on cannot loop. Filters:
  `channel_id`, `type`, `user_id`, `subtype`, `text` …
- Palette chip + live trigger sample (`channels → conversations.history`),
  signing secret over `dapier connections import --signing-secret-file` and
  the console Manage-connection dialog; replay works like every connector
  event (recorded run envelopes).

## Round 7, 2026-09-28: per-event trigger samples — dropbox file events

- A multi-event connector's sample pull now serves each declared event its
  own payload (`trigger_discovery.per_event_sample_fetch`): dropbox answers
  `file.created` / `file.updated` / `file.deleted` with exactly the shapes
  `triggers.intake.dropbox_resolver.process_entry` publishes — file state
  for created/updated, path-only for deleted — and recorded history only
  fills a sample when its replayed envelope carries the asked event.
- Tests: `tests/test_trigger_variety_dropbox.py` (per-event payloads,
  fallback for unknown events, history never crosses events).
- Remaining trigger-variety gap: zoom declares four events but every ask
  still gets the one recording.completed sample renamed; other connectors
  declare a single event, where renaming is correct.

## Round 8, 2026-09-28: per-event trigger samples — zoom recording + meeting events

- Zoom's sample pull moved into its connector module (`connectors.zoom`,
  where the docstring already placed it) and serves each declared event its
  own payload via `per_event_sample_fetch`: `recording.completed`,
  `recording.transcript_completed` (MP4 + TRANSCRIPT files),
  `meeting.started`, `meeting.ended` — each mirroring exactly what the
  webhook intake publishes, metadata only.
- The intake (`triggers.intake.zoom_webhooks`) accepts all four events now
  (it dropped everything but `recording.completed`): transcript deliveries
  reuse the recording shape with the TRANSCRIPT file included; meeting
  lifecycle deliveries flatten `payload.object` to the scheduling facts a
  workflow templates over (topic, host, start/end, duration, timezone) —
  participants and settings stay out of runs. Dedup ids include the event
  name, so a meeting's started and ended deliveries never collide.
- Tests: `tests/test_trigger_variety_zoom.py` (per-event payloads, unknown-
  event fallback, history never crosses events) and the intake variety in
  `tests/test_zoom.py` (transcript file, metadata-only meeting envelopes,
  distinct event ids, unsubscribed events dropped).
- Trigger-variety gap: closed — every palette connector now declares exactly
  the events its sample pull serves and its intake publishes.
