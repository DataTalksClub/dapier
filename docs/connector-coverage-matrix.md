# Connector coverage matrix — discovery / replay / test

Audit of the Zapier-parity surfaces (browse live resources, replay past
events, test credentials) across every connector, dumped live from the
registry (`registry.ACTIONS / DISCOVERIES / CONNECTION_TESTS`,
`trigger_discovery.TRIGGER_DISCOVERIES`). Regenerate the raw dump with
`.tmp/coverage-audit/dump_registry.py` → `.tmp/coverage-audit/registry_dump.json`.

## Providers (connection-capable): 8/8 tested, 8/8 discoverable

| Provider | ConnectionTest | Discovery sources | Notes |
|---|---|---|---|
| google | ✅ (`sheets.py`, key `google`) | google-sheets, google-drive, google-calendar, gmail | one OAuth connection serves all four |
| youtube | ✅ (`youtube.py`) | youtube | |
| zoom | ✅ (`zoom.py`) | zoom | |
| dropbox | ✅ (`dropbox.py`) | dropbox | |
| slack | ✅ (`slack.py`) | slack | token provider |
| telegram | ✅ (`telegram.py`) | telegram | token provider |
| mailchimp | ✅ (`mailchimp.py`) | mailchimp | credential, not a connection record — pseudo-connection |
| aws | ✅ (`s3.py`, key `aws`, alias `s3`→`aws`) | s3 | credential, not a connection record — pseudo-connection |

`PROVIDER_DISCOVERY_SOURCES` covers every provider; every discovery
connector resolves from at least one provider key. Test endpoint
`POST /api/agent|admin/connections/{id}/test`, CLI `dapier connections test`,
console Credentials "test" (`views/connections.js`). Discovery endpoints
`GET …/connections/{id}/discover[/{resource}]` on agent + admin, CLI
`dapier connections discover`.

## Discovery resources: 28 registered

| Resource | Consumed by action field hints |
|---|---|
| slack.channels | `slack.channel`, `slack_add_reaction.channel`, `slack_invite_to_channel.channel`, `slack_pin_message.channel`, `slack_schedule_message.channel`, `slack_set_purpose.channel`, `slack_set_topic.channel`, `slack_update_message.channel`, `slack_upload_file.channel` |
| slack.users | `slack_dm.user_id`, `slack_find_user.email`, `slack_invite_to_channel.users` |
| slack.messages | `slack.thread_ts`, `slack_add_reaction.timestamp`, `slack_pin_message.timestamp`, `slack_schedule_message.thread_ts`, `slack_update_message.ts`, `slack_upload_file.thread_ts` |
| telegram.chats | all nine `telegram_*` actions' `chat_id` (send, send_document, send_photo, send_poll, find_chat, edit_message, pin_message, ban_member, unban_member) |
| dropbox.folders | `dropbox_upload.folder`, `dropbox_find.path` |
| dropbox.files | `dropbox_delete.path`, `dropbox_read_file.path`, `dropbox_get_temp_link.path`, `dropbox_move.from_path`, `dropbox_copy.from_path` |
| dropbox.search | `dropbox_find.query` |
| google-drive.files | `drive_copy_file.file_id`, `drive_delete_file.file_id`, `drive_find_file.name`, `drive_move_file.file_id`, `drive_read_file.file_id`, `drive_share_file.file_id`, `s3_upload.source_url` |
| google-drive.folders | `drive_create_folder.parent_folder_id`, `drive_find_file.folder`, `drive_move_file.add_parent`/`remove_parent`, `drive_upload_file.folder_id` |
| google-sheets.spreadsheets / worksheets / columns | every `sheets_*` action (spreadsheet → worksheet → column cascades, incl. `sheets_delete_row`) |
| google-sheets.rows | `sheets_update_row.row`, `sheets_delete_row.row` |
| google-calendar.calendars | all five `calendar_*` actions' `calendar_id` (create, delete, find_events, quick_add, update) |
| gmail.labels | **browse-only** |
| google-calendar.events | **browse-only** (a calendar's events) |
| s3.buckets / s3.objects | `s3_upload`, `s3_find`, `s3_list_objects`, `s3_read_object`, `s3_delete_object`, `s3_presign_url` (bucket → key cascade; objects takes an optional `prefix` param), `render_html_to_pdf.output_bucket` |
| mailchimp.audiences / mailchimp.members | all five member actions (`upsert`, `find`, `tag`, `unsubscribe`, `remove`): `list_id` from audiences, `email` from members (scoped by the picked `list_id`) |
| youtube.videos | `youtube_add_to_playlist.video_id`, `youtube_update_video.video_id` |
| youtube.playlists | `youtube_add_to_playlist.playlist_id`, `youtube_find_playlist_items.playlist_id` |
| youtube.playlist_items | **browse-only** (the `youtube_find_playlist_items` data source) |
| youtube.channel | **browse-only** (static "connected channel" fact, nothing to select) |
| zoom.meetings | `zoom_add_registrant.meeting_id`, `zoom_find_meeting.meeting_id`, `zoom_update_meeting.meeting_id` |
| zoom.past_meetings | `zoom_delete_meeting.meeting_id`, `zoom_list_past_participants.meeting_id` |
| zoom.recordings | `zoom_find_recording.meeting_id`, `zoom_delete_recording.meeting_id` |
| zoom.webinars | `zoom_add_webinar_registrant`, `zoom_find_webinar`, `zoom_delete_webinar`, `zoom_list_past_webinar_participants` (`.webinar_id`) |

Browse-only resources still work end-to-end (CLI/console list + items); they
lack a designer field that would use them. The four: `gmail.labels` and
`youtube.channel` are static facts about the connection, `google-calendar.events`
lists a calendar's events (the calendar actions take ids picked from
`calendars`), and `youtube.playlist_items` is `youtube_find_playlist_items`'s
data source (`youtube_remove_from_playlist` takes a raw `playlist_item_id`,
copyable from that action's output via templates).

## Actions: 105 registered, 75 with discover hints

Every action with a connection-bound *selection* field carries a `discover`
hint. The 30 without, grouped (all acceptable):

- **No provider-backed selection at all** (compute, transport, email):
  `agent`, `ai_complete`, `code`, `js`, `csv_parse`, `csv_format`,
  `http_request`, `webhook`, `email_send`, `gmail_send`, `dataops`,
  `digest_add`, `digest_flush`, `run_workflow`.
- **Storage keys are free** (G12/G13): `storage_get`, `storage_set`,
  `storage_delete`, `storage_find`.
- **Create-shaped** (they name things into existence, nothing to select):
  `dropbox_create_folder`, `sheets_create_spreadsheet`,
  `slack_create_channel`, `youtube_create_playlist`, `youtube_upload_video`,
  `zoom_create_meeting`, `zoom_create_webinar`.
- **Free-text or raw identity**: `slack_find`, `slack_find_message`,
  `youtube_find_video` (queries); `youtube_remove_from_playlist` (raw
  `playlist_item_id`, templateable from `youtube_find_playlist_items`
  output); `slack_add_reminder` (text + time for yourself).

Designer catalog mirror (`designer/src/catalog.ts`) is in sync with the
registry — `tests/test_designer_pickers.py` green; bundle
(`src/web/designer.js`) rebuilt after the last catalog edit.

## Triggers: 17/18 palette connectors have sample discovery

18 palette connectors (ai, custom, dropbox, email, gmail, google-calendar,
google-drive, google-sheets, mailchimp, poll, renderer, rss, s3, schedule,
slack, telegram, youtube, zoom) declare 43 events. 17 register
`TriggerDiscovery(kind="sample")`, live where the provider allows (slack:
the newest channel message, delivery-shaped via
`triggers.intake.slack_events.event_data`), else history-first with
synthetic fallback — and multi-event connectors (dropbox, zoom, email,
google-drive, s3, telegram, slack, mailchimp) serve one documented payload
per declared event. `ai` is action-side only (`ai_complete`): it declares
no events and needs no sample. Field options (`kind="options"`): all 28
registry discovery resources plus `poll.triggers` / `schedule.triggers`
(30 option listings; the set-equality invariant is pinned in
`tests/test_options_breadth.py`). Extras beyond the palette: dataops,
webhook — used by trigger setup flows.

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
   (round 5, below); `slack.messages` left the browse-only set when the
   message actions gained `thread_ts`/`ts`/`timestamp` pickers (six fields).
   The current browse-only four — `gmail.labels`, `google-calendar.events`,
   `youtube.channel`, `youtube.playlist_items` — stay so by design (see the
   resources section).
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
- Designer: the "Insert from previous steps" output picker
  (`{steps.<id>.output.*}` chips).
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

## Round 9, 2026-09-28: full re-dump — 105 actions, 28 discovery resources

The factual sections above were regenerated from a fresh
`.tmp/coverage-audit/registry_dump.json` (105 actions, 18 trigger
connectors, 28 discovery resources, 8 connection tests, 49 trigger
discoveries). Since round 8 the connectors grew the google-calendar and
gmail resources (browse-only `google-calendar.events`, `gmail.labels`),
mailchimp member actions, and the slack message-timestamp pickers — the
resource table and the no-hint action list now say exactly that. The
75/105 hint split and the four browse-only resources are the only
remaining follow-ups worth naming, and both are deliberate.
