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
| google-drive.folders | **browse-only** — no consuming field yet |
| google-sheets.spreadsheets / worksheets / columns | all four `sheets_*` actions (spreadsheet → worksheet → column cascades) |
| google-sheets.rows | `sheets_update_row.row` |
| s3.buckets / s3.objects | `s3_upload`, `s3_find` (bucket → prefix/key cascade; objects takes an optional `prefix` param) |
| zoom.meetings | `zoom_find_meeting.meeting_id` |
| slack.users, slack.messages | `slack_find_user.email` (slack.users picker); slack.messages **browse-only** |
| youtube.channel / playlists / playlist_items | **browse-only** |
| zoom.recordings | **browse-only** |

Browse-only resources still work end-to-end (CLI/console list + items); they
lack a designer field that would use them. Candidates: `zoom.recordings` → a
future "Zoom: fetch recording" download action; `youtube.playlists` /
`playlist_items` → video-download or playlist-trigger config;
`slack.messages` → message templates.

## Actions: 30 registered, hint coverage

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

## Triggers: 8/8 palette connectors have sample discovery

custom, dropbox, email, poll, renderer, schedule, youtube, zoom — all
register `TriggerDiscovery(kind="sample")`, history-first with synthetic
fallback. Field options (`kind="options"`): dropbox folders, s3 buckets,
s3 objects (bucket passed as `event`), slack channels, telegram chats,
zoom meetings, google-sheets spreadsheets, google-drive files. Extras beyond
the palette: dataops, webhook — used by trigger setup flows.

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
2. **Browse-only discoveries** (six resources, table above — now including
   `google-drive.folders`) — reachable, but no designer field consumes them;
   wire when the matching actions land.
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
