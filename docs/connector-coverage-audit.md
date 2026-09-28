# Connector coverage audit — 2026-09-28 (re-verified against the working tree)

> **Update, later the same day — all four ranked gaps below are CLOSED.**
> Google Sheets/Drive and S3 are first-class trigger chips on a pluggable
> poll-source seam (`triggers/poll_sources.py`; sources `google-sheets.rows`,
> `google-drive.files`, `s3`, `zoom.recordings`), and the dropbox bytes gap
> closed with `dropbox_read_file` + `dropbox_get_temp_link`. Details at the
> bottom. Mailchimp intake landed earlier the same day (`triggers/intake/`).

Re-audit of "discover it, replay it, test it — for every connector" against the
live catalog (`registry.catalog()` + `trigger_discovery_catalog()` dumped from the
working tree) rather than the doc. The gap-analysis matrix from earlier today was
accurate at the time; the writer landed more while this audit ran. Everything
below is verified live, not copied.

## Verified live at the working-tree tip

- **Chips (14):** email, youtube, dropbox, zoom, slack, telegram, **mailchimp
  (new this round: 6 webhook-type events)**, renderer, schedule, poll, custom,
  google-sheets, google-drive, s3 (the last three landed as poll-source chips
  after this count was first taken — see the closed gaps below).
- **Sample pull:** every chip + `dataops` (action-side, deliberate) — history
  fallback chain confirmed in code, not just tests.
- **Discovery resources:** google-drive (files, folders), google-sheets
  (spreadsheets, worksheets, columns, rows), youtube (channel, playlists,
  playlist_items, **videos** — the channel's recent uploads, so the
  add-to-playlist and update-video `video_id` fields pick live), dropbox
  (files, folders, search), zoom (meetings, recordings), slack (channels,
  users, messages), telegram (chats), s3 (buckets, objects), mailchimp
  (audiences, members). Slack's `messages` resource now also backs the
  `thread_ts`/`ts`/`timestamp` fields on message, schedule, upload,
  update and reaction actions (pick the parent message from the channel's
  recent history).
- **Connection tests:** google, youtube, dropbox, zoom, slack, telegram,
  s3/aws (pseudo connection), mailchimp (pseudo connection).
- **Find actions:** sheets find/lookup/update row, drive find file, dropbox
  find, s3 find, slack find user/channel + find user by email, telegram find
  chat, youtube find video + playlist items, zoom find meeting + recording,
  mailchimp find member + upsert member; create actions: mailchimp upsert
  member, zoom create meeting (scheduled or instant), youtube upload video
  (the last write-action gap; needs the `youtube.upload` OAuth scope — see
  docs/connectors/google.md), drive upload file.
- **Replay + test are platform-level**, so they cover every connector by
  construction: full replay, replay-from-step, inbox replay, per-step
  "Test step", trigger-sample autofill, connection test.
- Poll triggers refresh OAuth tokens before fetching (`poll_triggers.
  _bearer_token` → `tokens.get_access_token`), so Google APIs are pollable
  today through a stored poll trigger; the seen-store dedupe applies.

## Fixed during this audit

**Youtube sample pull 502'd instead of falling back to the synthetic sample**
(`test_youtube_sample_carries_the_pubsub_entry` red at tip).
`trigger_discovery.connected_connection` read `os.environ["CONNECTIONS_TABLE"]`
unguarded; `api.discovery._load` deliberately tolerates a missing table. Added
the same guard — no table configured now means "nothing live to discover"
(404 → history/synthetic fallback), never a KeyError wrapped as 502. Targeted
set (trigger samples + all discovery + CLI discovery) now 117 passed.

## In flight (writer landed mid-audit — confirm before merge)

- Mailchimp chip + per-type samples + audience picker + conn test landed in
  `connectors/triggers.py` / `mailchimp.py` / the designer mirror.
- Workflow delete (`designer_store.api_delete` with delayed-run gate),
  webhook `response: {mode: sync|ack|challenge}`, and workflow tags all
  appeared in the tree during this audit — the gap-analysis doc's open
  findings 5/6/8 are closing; the doc needs a sweep once the writer settles.

## Remaining gaps, ranked — CLOSED (2026-09-28, same day)

1. **CLOSED — Mailchimp intake.** `triggers/intake/mailchimp_webhooks.py` is
   wired: `/hooks/mailchimp/<name>` (api/router.py) parses the form-encoded
   delivery into `{type, data}` events matched by the mailchimp chip, with
   hook-save guidance; `hook_triggers.KINDS` includes `mailchimp`.
2. **CLOSED — Google Sheets / Drive trigger chips.** The palette has
   `google-sheets` (`row.new`) and `google-drive` (`file.created`) chips
   backed by poll sources: a stored poll with `source: "google-sheets.rows"`
   (spreadsheet_id, optional worksheet, google connection required) or
   `"google-drive.files"` (folder_id + connection) fetches through the
   connection's OAuth token on the poll schedule. First fire seeds the
   cursor without emitting (no history backfill); later fires emit only new
   rows/files, deduped via the seen store. Sample discovery: live through
   the named stored poll, else history, else synthetic.
3. **CLOSED — S3 trigger.** The poll source seam (`triggers/poll_sources.py`)
   lets a stored poll swap the HTTP+JSON fetcher for a registered provider
   fetch; `source: "s3"` lists the bucket through boto3 ListObjectsV2
   (bucket required, prefix/credential_id optional, aws shared credential by
   default). Composite item ids (`last_modified|key`) keep same-second
   objects distinct; new objects only, oldest first. `s3` chip in the
   palette; S3 chip sample follows the same live→history→synthetic chain.
4. **CLOSED — dropbox bytes.** `dropbox_read_file` stages a file's bytes for
   later steps (pairs with `s3_upload` `source_s3`), and
   `dropbox_get_temp_link` mints a direct download link a following
   `s3_upload` consumes as `source_url`. (`zoom_create_meeting` had already
   closed the Zoom half while the audit ran.)
5. **CLOSED — Zoom recordings trigger, no Zoom app required.** The
   `zoom.recordings` poll source lists the connection's cloud recordings
   (`/users/me/recordings`, optional `for_email` mailbox) on the schedule
   machinery and publishes the same `zoom`/`recording.completed` events the
   webhook intake delivers — the composite `start_time|uuid` watermark keeps
   same-minute recordings distinct, the first fire seeds without emitting,
   and the MP4/M4V rule matches `zoom_webhooks`. The Zoom chip's sample pull
   is now poll-aware: an `event` naming a stored poll answers live (poll ids
   cannot contain dots, so per-event asks never collide), else the
   documented per-event chain. Tests: `tests/test_zoom_poll.py`.

Surfaces: the chips and the per-source fields are in the designer mirror
(`designer/src/catalog.ts`, rebuilt) and the console Triggers view (Source
select + provider params through the options JSON); the CLI rides the same
`/api/agent/poll-triggers` body (`dapier polls save` with `source` in the
file), and `polls list/save` print the provider target. Tests:
`tests/test_poll_sources_seam.py` (seam contract), `tests/test_s3_trigger.py`,
`tests/test_google_triggers.py`, `tests/test_zoom_poll.py`,
`tests/test_action_breadth.py`.

## Sample-chain corrections — 2026-09-28, later (two audit findings)

The blanket "every chip: live → history → synthetic" claim above needed two
repairs; both landed so the claim is now true without exceptions worth
caveats:

- **Custom chip sample drift.** The `custom` connector's synthetic sample
  declared event `occurred`, but the only fire path that publishes the
  `custom` connector is `/hooks/custom/{source}`, which publishes
  `custom`/`received` (`api/router.py`) — and matching is exact on
  connector+event, so a filter copied from the sample never matched. The
  sample (and any doc naming the event) now says `received`; fired names
  are untouched, so stored filters keep working. The chip's other backing,
  the webhook trigger kind (`webhook`/`request.received`), already matched
  its sample. Test: `test_custom_chip_samples_name_events_that_actually_fire`
  fires both paths and asserts each sample's event equals the published one.
- **Telegram and poll sample tails.** Telegram was live-or-404/502 (a
  webhook-mode bot's `getUpdates` 409 — its normal dapier state — 502'd,
  and no pending updates 404'd); the poll chip 404'd when no run was
  recorded. Both now fall through history → synthetic like the other
  chips (`connectors/telegram.py`, `connectors/poll.py`). Two poll asks
  stay deliberately precise, not chain steps: a *named* trigger whose
  fetch fails is still a 502, and a name matching no stored poll trigger
  is still a 404 rather than a fabricated sample that hides the typo.

## Fresh parity audit — 2026-09-28, evening (working tree, post round 13)

Dispatched once the dropbox-file-read lane landed: discover / replay / test
re-verified live for every connector, plus three parallel sweeps (action
breadth, trigger variety + provider lifecycle, platform + UI/CLI parity)
against a live `registry.catalog()` / `trigger_discovery_catalog()` dump.
Two findings the sweeps reported were already closing as they read; both
re-verified fixed in the tree before this section was written.

### Verified closed — no action needed

- **Discover/replay/test for all connectors** — platform-level and
  universal: all 14 chips + `dataops` sample-pull (live → history →
  synthetic), full replay, replay-from-step, inbox replay, per-step
  "Test step", connection tests.
- **Mailchimp webhook lifecycle** — wired both directions:
  `_register_mailchimp`/`_unregister_mailchimp` fire from `api_save` and
  `api_delete` (`triggers/hook_triggers.py`; Mailchimp failures warn
  without blocking the local change), with Mailchimp-API-call tests in
  `tests/test_hook_triggers.py`. (An earlier audit snapshot showed them
  uncalled — landed mid-audit.)
- **`zoom.recordings` poll source** — landed mid-audit (section above).
- **UI/CLI parity** — holds: every mutating console call has an
  `/api/agent/*` twin and a CLI command; admin-only endpoints are
  read-only or browser-flow by nature (`me`, designer catalog, oauth
  start/login/logout/callback).
- Everything previously marked closed (rounds 1–13, the round-5
  "Surprises" re-checks) re-verified standing.

### Remaining gaps, ranked (value × effort; all evidence-checked)

Behavior / trigger gaps:

1. **YouTube WebSub initial subscribe** — the hub subscription happens
   only in the renewal Lambda (`triggers/intake/youtube_subscriptions.py`,
   scheduled `rate(5 days)`, `template.yaml:469`), so a brand-new youtube
   trigger can sit silent for up to 5 days. Fix: subscribe on trigger
   save, or shorten the rate. Value H, effort S.
2. CLOSED (2026-09-28) — Slack event variety: `slack_events.event_name`
   maps each subscribed Slack type to its own workflow-facing event —
   `message.received`, `app.mention`, `reaction.added`, `member.joined` —
   and `docs/connectors/slack.md` now carries the signing-secret +
   Request-URL setup and the per-event list (see the gap-analysis doc's
   item 7 for the full landing). Tests:
   `tests/test_slack_event_variety.py`, `tests/test_slack_events.py`.
3. **google-drive `file.updated`/`file.deleted`** — Drive `changes.list`
   page tokens map 1:1 onto the existing `next_cursor` poll machinery.
   Value M-H, effort M.
4. **Two more poll sources on the seam** — `youtube.uploads`
   (uploads-playlist items; survives WebSub lease lapses) and
   `mailchimp.members` (`last_changed` watermark): each ~1 validate + 1
   fetch function on the `PollSource` contract. Value M each, effort S.
5. Low priority: telegram distinct `channel_post`/`callback_query`;
   sheets `row.updated` (snapshot diff — the Sheets API has no row
   timestamps). CLOSED (2026-09-28, later): **S3 `file.updated`/
   `file.deleted`** — the earlier "push-shaped, skip" call was wrong on
   both counts: Zapier's S3 trigger has no update/delete pair either, but the
   poll seam never needed a feed. `s3.updates`/`s3.deletions` diff
   consecutive ListObjectsV2 listings — the drive changes-sources' pattern
   with the previous listing parked in the cursor (etag|size|last_modified
   fingerprints; new keys stay the created source's; deletion events carry
   the key alone). A changed prefix, a capped listing on a deletions watch,
   or an unreadable snapshot re-seeds without firing — false deletions are
   worse than late ones. The S3 chip's sample pull answers per event
   (updates poll live = the bucket's newest object as an update envelope;
   deletions has no live answer and falls to history/synthetic). Tests:
   `tests/test_trigger_variety_s3.py`.

Action breadth (each = register + runner + designer mirror; the
per-provider transport plumbing all exists):

6. CLOSED (2026-09-28, parity lanes): every action on this list is
   registered (`connectors/*.py`) with an engine runner and a designer
   catalog mirror — `sheets_delete_row`, `sheets_create_spreadsheet`,
   `mailchimp_remove_member`/`mailchimp_tag_member`,
   `dropbox_move`/`dropbox_copy`, `slack_update_message`,
   `slack_create_channel`, `telegram_send_photo`, `s3_delete_object` +
   `s3_presign_url`, `zoom_update_meeting`/`zoom_delete_meeting`,
   `youtube_add_to_playlist`. CLOSED too (2026-09-28, quick-actions lane):
   mailchimp unsubscribe — `mailchimp_unsubscribe_member` (PATCH status →
   unsubscribed, reversible where remove is permanent; upsert's
   `status_if_new` cannot unsubscribe an existing member), engine +
   registry + catalog + `tests/test_mailchimp_actions.py`.
7. CLOSED (2026-09-28, media-actions lane): `slack_upload_file` (v2 upload
   flow: getUploadURLExternal → binary POST → completeUploadExternal) and
   `drive_read_file` (stages bytes like `dropbox_read_file`, exporting
   Google-native docs via `files.export` first; `drive_upload_file` had
   already landed). Tests: `tests/test_slack_upload_file.py`,
   `tests/test_drive_read_file.py`. CLOSED too (2026-09-28, mirror-parity
   sweep): email attachments (SES RawMessage) + reply-to/cc/bcc — engine,
   `tests/test_email_attachments.py`, and the designer mirror all carry
   them.
8. CLOSED (2026-09-28): `http_request` returns lowercased response
   headers in `response_headers` (`connectors/webhook.py`), and
   `create_if_missing` now covers every find action with a meaningful
   create: `sheets_find_row`, `sheets_lookup_row`, `dropbox_find`,
   `zoom_find_meeting`, `zoom_find_recording`,
   `mailchimp_find_member` (upsert on a miss; the quick-actions lane),
   and `slack_find`'s channel branch (conversations.create; the
   quick-actions lane). The remaining finds name content-bearing objects
   (drive/youtube files, s3 objects) or immutable identities (users,
   chats) — creating on a miss would mean uploading payloads or
   inventing accounts, which is not a find-or-create.

Platform / editor (organizational, deliberately deferred until now):

9. **Multi-user roles / shared workspaces** — the largest structural gap:
   authz is a single operator allowlist plus per-connection grants; no
   user/role/collaborator model anywhere. Value H, effort L-M.
    CLOSED (2026-09-28, v1 — shared workspaces still open): stored user
    roles in `RoleAssignmentsTable` (`auth/roles.py`), keyed by DTC subject
    or email: `viewer` reads the console (overview, runs, audit, designer
    lists, discovery), `editor` also edits and tests workflows plus run
    replays/cancels, `operator` is the full console, `admin` also manages
    users, and a `disabled` flag denies an account everywhere. A stored row
    is authoritative — it widens a non-allowlisted identity or narrows a
    stored-down operator — and with no row the operator allowlist decides
    exactly as before (an empty store keeps allowlist operators as
    bootstrap admins, so the first assignment is always reachable). The
    least-privilege band is classified per route (`minimum_for_route`) and
    per CLI action (`minimum_for_action`); anything unclassified stays
    operator. Management on all three surfaces: `/api/admin/users` +
    `/api/agent/users` (list / set-role / remove, `users.set-role` /
    `users.remove` audit rows, a last-admin lockout guard), `dapier users
    list|set-role|remove`, and the console's Users view; `/api/admin/me`
    carries the effective role. Tests: `tests/test_roles.py`.
10. **Templates / marketplace** — publish, browse, fork shared workflows.
    CLOSED (2026-09-28, v1): a `template: true` definition flag feeds a
    gallery (console designer + `dapier templates list`), apply forks the
    template through the save path, and publish/unpublish is one flag
    endpoint — all on console, CLI, and API. Cross-account transfer remains
    blocked on 9.
11. **Runs CSV export** — CLOSED (2026-09-28): `runs_to_csv` + export on
    `api/runs.py`, `dapier runs export` (`-o` file), console Runs view
    export — all riding the same bounded-window API.
12. **Designer editor polish** — CLOSED (2026-09-28): undo/redo (see CLOSED
    below), step duplication (context menu + inspector button + Ctrl/Cmd+D),
    copy/paste step across workflows (Ctrl/Cmd+C → Ctrl/Cmd+V, clipboard
    survives the workflow switch), Delete-key node removal, and a "?"
    keyboard cheat-sheet dialog. Editor-local until save — presentational
    exception, no API surface needed.

## CLOSED — whole-editor undo/redo in the designer (2026-09-28)

The designer's undo history used to live inside the board component, so it
captured canvas drags and deletes but not a single inspector edit — and an
inspector keystroke could be silently discarded by the next board undo. All
editing now shares one timeline (`designer/src/history.ts`, wired in
`App.tsx`): canvas ops (add/connect/delete/rename/reattach/action-type
change/clear), inspector field edits (coalesced per node, so a typing burst
is one undo step), workflow renames, the On/Off toggle, YAML→canvas
materializations, copilot-draft loads, and drag moves (one step per drag).
Ctrl+Z / Ctrl+Shift+Z / Ctrl+Y work outside text fields, with toolbar
buttons that disable at the timeline ends; opening or creating a workflow
restarts the history. Rebuilt into the console bundle with
`make designer-console`.

## CLOSED — poll sources for the webhook-only chips (2026-09-28, later still)

The two connectors whose triggers could only arrive by webhook gained poll
paths on the existing seam, in the `google-sheets.rows`/`s3` mold (stored
poll, seeded first fire, strictly-new watermark, seen-store dedupe):

- **`dropbox.files`** (`dropbox`/`file.created`) — `path` (folder) +
  `connection_id` required; the fetch lists the folder through the
  connector module's own listing (`_run_entries`, files/list_folder +
  continue, so the poll sees exactly what the folder pickers browse) and
  watermarks on `server_modified`, oldest first. A Dropbox app with a
  verified webhook is no longer the only way to fire a dropbox trigger.
- **`youtube.videos`** (`youtube`/`video.published`) — `connection_id`
  required, `channel_id` optional (empty resolves to the connection's own
  channel through channels.mine, the same resolution the sample pull uses);
  the fetch lists the channel's uploads playlist (`UC`→`UU` swap, the
  shared provider layer) and watermarks on `publishedAt`, oldest first,
  items flattened to the PubSubHubbub notification's data shape so
  downstream templates read the same keys on both fire paths. This closes
  the youtube half of gap 4 in the evening audit above (named
  `youtube.videos` after the chip's event, not `youtube.uploads`); a
  lapsed WebSub lease no longer silences a youtube trigger.

Both chips' sample pulls are poll-aware: an `event` naming a stored poll of
that source answers live through the poll's own fetch against an epoch
watermark — no stored cursor read or advanced — and anything else falls
through to the existing webhook-side chain (history → documented example),
which keeps its per-event semantics. Poll ids cannot contain dots, so
per-event asks never collide with stored polls. The dropbox registry
lambdas (upload/delete/find/read_file/temp-link) now pass a `transport`
kwarg through to their engine runners like the module's discovery runners
already did — no behavior change (the engine dispatch leaves it unset).

Surfaces: `poll_sources._BUILTIN_MODULES` carries both modules; the console
Triggers view Source select lists them (connection required, provider
params through the options JSON: `path`, `channel_id`); the CLI rides the
same `/api/agent/poll-triggers` body. The designer mirror needed no change —
the dropbox and youtube chips already exist in `connectorCatalog` and the
mirror carries no per-source poll fields for any source. Tests:
`tests/test_dropbox_poll.py`, `tests/test_youtube_poll.py`.


## CLOSED — the media actions and the find-or-create sweep (2026-09-28, latest)

Item 7's `slack_upload_file` and `drive_download_file`, and item 8's
`create_if_missing`, are closed (the drive download landed as
**`drive_read_file`**, named for its `dropbox_read_file` sibling):

- **`slack_upload_file`** — Slack's v2 simple upload:
  `files.getUploadURLExternal` (filename + byte length) → binary POST to the
  presigned URL (the URL is the credential) → `files.completeUploadExternal`
  (`files: [{id, title}]`, `channel_id`, optional `initial_comment` /
  `thread_ts`). Bytes come from exactly one of `source_url`, staged
  `source_s3`, or inline `content`. Token needs `files:write`. Output:
  `{ok, channel, file: {id, name, title, permalink}}`.
- **`drive_read_file`** — Drive `files.get` with `alt=media`, bytes staged to
  the artifacts bucket exactly like `dropbox_read_file` (output
  `{filename, size, content_type, bucket, key}` plus `file_id`/`name`);
  Google-native files export first (`export_as`, per-kind defaults). Pairs
  with S3's `source_s3` and Slack's upload so the read→send chain needs no
  HTTP at all.
- **`create_if_missing` on `zoom_find_meeting`** — the one remaining genuine
  find-or-create. A missed topic search creates the meeting from Topic plus
  the shared create fields (reusing `zoom_create_meeting`'s payload rules)
  and reports `{found: false, created: true, scheduled, meeting}`; a hit and
  a meeting-id lookup never create. Needs `meeting:write:meeting` for the
  create half.

Skipped, deliberately: `slack_find_user` (no user-create API — a workflow
invites people, it does not mint them), `drive_find_file` (a miss composes
with `drive_upload_file`; "creating" a Drive file to then find it adds a step,
not a capability — and the Sheets/Dropbox flag works because the create is a
named, side-effect-free placeholder), and `s3_find` (creating an empty object
to find is storage noise, not find-or-create). `mailchimp_upsert_member` and
`zoom_create_meeting` already cover their own upsert-shaped needs.

Tests: `tests/test_slack_drive_media.py` (the three-step upload, the
source_url staging path, the drive→Slack staged-bytes handoff, zoom
find-or-create, registry dispatch + `validate_action_chain`), plus the
existing `tests/test_drive_read_file.py` for the download action's unit
behavior.

## CLOSED — designer mirror parity sweep (2026-09-28, latest)

A programmatic sweep compared every registered action's canonical field
list (`connectors/registry.py` `Action.fields`, what the API/CLI surfaces
serve) against the designer's `catalog.ts` mirror: 73 registry actions + 6
logic-step types, field by field. The first pass caught real drift — the
engine had grown faster than the mirror:

- **`email_send`** — `cc`, `bcc`, `reply_to`, `attachments` (the raw-MIME
  attachment list, YAML dialect matching the registry placeholder).
- **`zoom_find_meeting`** — `create_if_missing` plus the create fields it
  consumes on a topic miss (`start_time`, `duration`, `timezone`,
  `agenda`, `settings`), description updated to say find-OR-create.
- **`slack`** (send) — `thread_ts` (reply into a thread).
- **`slack_schedule_message`** — `unfurl_links` / `unfurl_media`.
- **`s3_find`** — `next_token` (feed a truncated listing's continuation
  token back; description updated).

Re-run after the fixes: **no gaps** — every registry field is reachable
from the designer inspector, and the only mirror-only fields left are the
deliberate per-entry error-handler toggles (`on_error`/`on_fail`/
`error_actions`). Bundle rebuilt (`make designer-console`), `tsc` clean.

## CLOSED — round 6 (2026-09-28): options discovery everywhere, quick actions, trigger events

Every registry `Discovery` resource is reachable as a trigger-discovery
`kind=options` listing (set-equality invariant pinned in
`tests/test_options_breadth.py`), with `poll.triggers`/`schedule.triggers`
pickers so a live sample needs no stored-name typing. Quick actions:
`drive_move_file`, `drive_delete_file` `permanent`, `sheets_add_worksheet`,
`zoom_delete_recording`, `s3_list_objects`, and `in_reply_to`/`references`
threading on `email_send`. New trigger events with per-event discovery
samples: telegram `channel_post.received`, zoom
`meeting.registration_created`. Tests: `tests/test_options_breadth.py`,
`tests/test_quick_actions.py`, `tests/test_trigger_events_breadth.py`.

## CLOSED — final sweep of 2026-09-28: rss/csv console surfaces + gate green

Three registry↔surface drift items were the only red left once the parallel
lanes settled; all closed and the canonical `make test` is green at
**3206 passed, 42 subtests** (session started the day at 2394):

- **`rss` poll source console surface** — the Source select in
  `index.html` now offers "RSS feed", and `triggers.js` treats rss like the
  HTTP source for the URL field (`url` is sent, not blanked; the
  connection-required early check does not apply). The gate test
  (`tests/test_console_trigger_sources.py`) learned the third category:
  rss needs neither an OAuth connection nor a stored credential — it polls
  a public feed URL — so it is exempt from `CONNECTION_POLL_SOURCES`
  alongside the credential-backed sources.
- **`csv_parse` / `csv_format` designer mirror** — both actions are now in
  `designer/src/catalog.ts` (Table icon, fields matching the registry's
  `Action.fields`, including the `source_s3` staged-file ref and the
  `header_row` boolean), and the bundle was rebuilt
  (`make designer-console`).
- **Zoom webinar actions mirrored by their own lane** —
  `zoom_delete_webinar` / `zoom_list_past_webinar_participants` landed in
  the mirror minutes after registration; no drift remained.

Also this round: the poll-source seam tests now isolate the global
`SOURCES` registry (fresh dict via monkeypatch — suite growth had made the
shared-dict fixtures order-dependent), YouTube save-time WebSub
subscribe/unsubscribe got its never-raises contract pinned
(`tests/test_youtube_subscribe_on_save.py`), Slack event variety is
documented with signing-secret setup (`docs/connectors/slack.md`),
`http_request` returns `response_headers`, `email_send` gained
`reply_to`/`cc`/`bcc`/`attachments` (raw MIME with an explicit Message-ID),
and the UI/CLI parity audit re-verified clean against the new surfaces
(runs CSV export on console, `/api/agent/*`, and CLI).
