# Google connectors

Google Calendar, YouTube, Drive, Docs, and Sheets use a shared Google OAuth
client, but each Dapier connection has its own account binding, requested
scopes, and token. The Google Cloud project configuration allows the client to
request scopes; it does not grant them to an account until that account
completes consent.

See the [shared connector procedures](../../docs/connectors/README.md) for OAuth credential
rotation, CLI operations, and the general process for changing scopes.

## Current configuration

- Google Cloud project: **`dtcdev-click`**.
- Existing Google clients are shared at the project level. The dedicated
  Dapier client is a Web application named `Dapier`; leave the separate
  `DTC DEV Auth` client and its `auth.dtcdev.click` callback unchanged.
- OAuth callback: `https://dapier.dtcdev.click/oauth/callback`.
- Enabled APIs: Google Calendar API, YouTube Data API v3, Google Drive API,
  Google Docs API, and Google Sheets API.
- OAuth audience: External, **In production** as of 2026-09-27; Google
  verification is still pending.
- Test users: `alexey.s.grigoriev@gmail.com` and
  `alexey@datatalks.club`.
- The shared consent branding is named `DTC`; do not change project-wide
  branding just for Dapier.

The user approved enabling Drive, Docs, and Sheets APIs; adding the Workspace
scopes listed below; and adding `alexey@datatalks.club` as a test user. That
approval covers this project, the listed scopes and accounts, and the Dapier
callback. Get new direction before adding different accounts or OAuth scopes.

## Google Cloud scope configuration

1. Open the [Google Cloud credentials page](https://console.cloud.google.com/apis/credentials)
   and select project **`dtcdev-click`** in the project picker.
2. Under **APIs & Services → Library**, enable each required API before adding
   scopes. The picker may not offer scopes for APIs that are disabled.
3. Open **Google Auth Platform → Data access → Add or remove scopes**. Add the
   scopes Dapier connections need, then click **Add to table**, **Update**,
   and finally **Save** on the Data access page. The changes require all three
   commits. Keep the project scope list in sync with Dapier connection scopes;
   do not remove scopes still used by another client in the project.
4. To add an account as a test user, go to **Google Auth Platform → Audience
   → Test users → Add users**, type its address, press **Enter** to turn it
   into a chip, then click **Save**.
5. In **Google Auth Platform → Clients**, open the existing `Dapier` Web client
   or create it. In **Authorized redirect URIs**, add exactly:

   ```
   https://dapier.dtcdev.click/oauth/callback
   ```

   This callback is fixed by the stack's `OAUTH_CALLBACK_URL` and is used by
   both the console and CLI. Copy the client ID and secret, then store them
   immediately with the [credential rotation procedure](../../docs/connectors/README.md#updating-or-rotating-an-oauth-client-id-and-secret).
   Google does not show an existing client secret again.

The project's current Dapier scopes are:

| Scope | Purpose | Google classification |
|-------|---------|-----------------------|
| `https://www.googleapis.com/auth/calendar.freebusy` | Read free/busy availability | Sensitive |
| `https://www.googleapis.com/auth/calendar.events.owned` | Read and edit events owned by the user | Sensitive |
| `https://www.googleapis.com/auth/youtube.readonly` | Read the user's YouTube channel and published videos | Sensitive |
| `https://www.googleapis.com/auth/userinfo.email` | Identify the consenting account | Basic identity |
| `https://www.googleapis.com/auth/drive.readonly` | Read and download all files in Drive; accepted by the Drive Changes API | Restricted |
| `https://www.googleapis.com/auth/documents` | Read, create, edit, and delete all Docs for the user | Sensitive |
| `https://www.googleapis.com/auth/spreadsheets` | Read, create, edit, and delete all Sheets for the user | Sensitive |

There is no separate Drive scope for watching changes: `drive.readonly` is
accepted by the Drive Changes API, including `changes.watch`. A watch
notification only signals that changes are available; Dapier still needs code
to fetch and process the changes feed. Current Dapier implementation includes
Sheets row-append and find-row actions, but no general Docs editor or Drive
file watcher. Configuring OAuth scopes does not implement those workflow
features. See Google's [Docs scopes](https://developers.google.com/workspace/docs/api/auth),
[Sheets scopes](https://developers.google.com/workspace/sheets/api/scopes),
and [Drive change notifications](https://developers.google.com/workspace/drive/api/guides/manage-changes).

## The Sheets find-row action

`sheets_find_row` reads a worksheet (header row plus data) and returns the
first row whose cell under the match column equals the match value; with
**Create if missing** it appends the row from Row values instead of failing.
Later steps read the result through the steps context, shaped as
`{steps.<step id>.output.*}`: for a step with `id: find`, templates can use
`{steps.find.output.found}`, `{steps.find.output.row.Task}`,
`{steps.find.output.row_number}`, and, after a create,
`{steps.find.output.updated_range}`. A `filter` step on
`{steps.find.output.found}` gives found / not-found branching.

## The two Workspace account connections

Connections are separate by account. Changing a scope list does not update an
already issued refresh token; reconnect each account and accept the new
consent screen.

### Gmail: `google-calendar`

This connection keeps the
Calendar scopes and requests Drive, Docs, and Sheets scopes as well:

```powershell
uv run dapier connections edit google-calendar --scopes `
  https://www.googleapis.com/auth/calendar.freebusy `
  https://www.googleapis.com/auth/calendar.events.owned `
  https://www.googleapis.com/auth/drive.readonly `
  https://www.googleapis.com/auth/documents `
  https://www.googleapis.com/auth/spreadsheets `
  https://www.googleapis.com/auth/userinfo.email
```

Status: **connected** to `alexey.s.grigoriev@gmail.com`. Its granted scopes
include the Calendar, Drive, Docs, Sheets, and account-identity scopes listed
above. The connection is granted to `personal-scheduler` for `connect` and
`use`. Verify with `uv run dapier connections show google-calendar`.

### DataTalks.club: `google-sheets`

This connection
requests only Workspace scopes and account identity:

```text
https://www.googleapis.com/auth/drive.readonly
https://www.googleapis.com/auth/documents
https://www.googleapis.com/auth/spreadsheets
https://www.googleapis.com/auth/userinfo.email
```

Status: **connected** to `alexey@datatalks.club`, with Drive, Docs, Sheets, and
account-identity scopes granted. The connection is granted to `todo-cli` for
`use`. Verify with `uv run dapier connections show google-sheets --agent todo-cli`.

To reconnect or add a Google account, use the connection command while signed
in as the intended account:

```powershell
uv run dapier connections connect google-sheets --agent <agent-name>
```

Check the Google account on the consent screen before approving. Then inspect
the connection to verify which scopes the token actually received.

#### Sheets actions

The catalog ships two actions against this connection. **Google Sheets**
(`sheets_append_row`) appends rows after the worksheet's last row of data.
**Google Sheets (find row)** (`sheets_find_row`) returns the first row whose
cell under a header column matches a value: `found`, `row_number`,
`row.<Column>`, and `values` land in the step output, so later steps template
them as `{steps.<step-id>.output.row.Task}`. With **Create if missing**
enabled, a miss appends the row from *Row values* instead and the output
gains `created: true` — Zapier's find-or-create shape. The match column is
matched against the header row trimmed and case-insensitively; the match
value, trimmed and case-sensitively. Both fields offer discovery pickers
(spreadsheets, worksheets, header columns) on the connection.

#### Sheets poll triggers

The `google-sheets` chip is also a real trigger: two poll sources run on a
schedule and publish the chip's events, each scoped per trigger through its
poll-name filter. **New Spreadsheet Row** (`google-sheets.rows` →
`row.new`) watermarks the spreadsheet row number — rows are append-mostly,
so rows numbered past the cursor are the news. **New or Updated Spreadsheet
Row** (`google-sheets.updates` → `row.updated`) is a snapshot diff: the
Sheets values API exposes no per-row modified time, so the trigger's cursor
carries a JSON snapshot of row number → content digest, and a row that was
listed before and now hashes differently fires once with its current cells
— the same shape as a `row.new` item, with `id` = `<row>:<digest>` so a
re-edit publishes as fresh news while an already-run edit stays silent.
Both sources share the row.new options (spreadsheet, worksheet,
connection), seed silently on the first fire — enabling a trigger must not
fire the sheet's existing rows — and never fire for creations
(`row.new`'s news) or deletions. The row number is a position: inserting or
deleting rows renumbers the rows below, so a moved row can fire (or be
masked) — the same polling caveat both sources (and Zapier's Sheets
triggers) share.

#### Drive actions

The Drive actions run over any Google connection granted `drive.readonly` —
except **Upload file**: Dapier connections request only the read-only Drive
scope today, so an upload fails with the API's HTTP 403 until the project and
connection carry a Drive write scope (the same approval flow as the YouTube
upload caveat below). **Find file**
(`drive_find_file`) returns the most recent file whose name contains (or,
with Match *exact*, equals) the given name: `found`, `file.id`, `file.name`,
and `count` land in the step output; a miss is `found: false`, not an error.
**Upload file** (`drive_upload_file`) writes one file from `source_url`, a
staged `source_s3: {bucket, key}` object, or inline `content`. **Read file**
(`drive_read_file`) is the reverse direction: it downloads one file
(`files.get` with `alt=media`) and stages the bytes in the artifacts bucket,
so later steps move them on — the output
`{filename, size, content_type, bucket, key, file_id, name}` feeds Amazon
S3's `source_s3` (bucket/key take templates) or Slack's upload file
(`slack_upload_file` with `source_s3`) directly. `file_id` renders from the
event: chain `drive_find_file`'s `{steps.<id>.output.file.id}` or a
file.created trigger's id. Google-native files (Docs, Sheets, Slides,
Drawings) have no bytes to download; they export first through
`files.export`, with **Export as** naming the target mime (documents and
presentations default to PDF, spreadsheets to CSV, drawings to PNG).
**Share file** (`drive_share_file`) creates a permission (role
reader/commenter/writer for a user, group, domain, or anyone) and
**Copy file** (`drive_copy_file`) duplicates a file. `drive_find_file`
has no create-if-missing: a find that misses composes with
`drive_upload_file` when a workflow wants find-or-upload, and creating an
empty artifact to "find" is not find-or-create.

## Gmail

The **Gmail** chip (`gmail`) reads and sends the connection's own mailbox over
the shared Google connection. **New Email** (`gmail.messages` →
`message.received`) is a poll source keyed on each message's `internalDate` —
the Drive files watermark applied to mail: the first fire seeds the watermark
and emits nothing (the mailbox is history, not news), later fires emit
messages delivered strictly after it, oldest first, deduped by the seen
store. An optional query scopes the watch — a `label:` term from the labels
picker, or any Gmail search expression. One fire expands at most ~100
message stubs (two pages of 50); mail beyond that budget waits for a quieter
mailbox. **Send email** (`gmail_send`) posts raw MIME through
`users.messages.send`; Gmail only delivers from the authenticated user (or a
verified alias), so the sender is always the connection itself, resolved
from `users/me/profile`.

Scope caveat: the chip declares `gmail.readonly` (messages, labels) and
`gmail.send` in `connectors/gmail.py` — both Google-**Restricted** scopes the
project does not request yet. Enabling Gmail end to end follows the same
steps as the YouTube upload scope below: enable the Gmail API in
`dtcdev-click`, add both scopes to the project's scope table with the owner's
approval, grant them to the Google connection with
`dapier connections edit <id> --scopes …`, and re-consent the account.
Until a connection holds the scopes, its Gmail poll and send fail with the
API's HTTP 403. The `labels` discovery resource needs only `gmail.readonly`.

### Discovery resources

Google connections list live resources over `dapier connections discover <connection>`
and the console/CLI pickers: `spreadsheets`, `files`, `folders` (Drive folders),
`worksheets`, `columns`, `rows` (the first rows of one worksheet, with
`spreadsheet_id` and optional `worksheet` params; trailing empty cells are
trimmed and each row keeps its spreadsheet row number), and `labels` (the
Gmail labels of the connection, behind the Gmail trigger's query picker).

### Calendar, YouTube, and other accounts

- Create **Google Calendar → Create new** in the console. It requests
  `calendar.freebusy`, `calendar.events.owned`, and `userinfo.email` by
  default. Approve from the Google account that should own this connection.
- Create **YouTube → Create new** and approve `youtube.readonly`. Dapier
  verifies the channel. If it finds no channel, use the Brand Account that
  owns it and reconnect.
- If the Google account has no usable refresh token yet, complete the consent
  flow once even if the client is already configured.

## YouTube video push (WebSub)

The `youtube` / `video.published` trigger fires from YouTube's PubSubHubbub
push feed, delivered to `https://dapier.dtcdev.click/hooks/youtube`. Connecting
the channel alone subscribes nothing: a channel is subscribed when a youtube
workflow item names it.

1. Connect the channel (OAuth, `youtube.readonly`) as described above.
2. Create a workflow item with `connector: youtube`,
   `event: video.published`, and a `channel_id` filter (for example
   `channel_id: {equals: UC…}`). A renewal schedule runs every four days and
   re-subscribes every channel named this way, passing the stored hub secret so
   deliveries arrive signed (`X-Hub-Signature: sha1=…`) and Dapier verifies
   them.
3. To start watching before the next renewal run, subscribe the channel on the
   hub directly:

   ```sh
   curl -d hub.mode=subscribe -d hub.verify=async \
     -d hub.callback=https://dapier.dtcdev.click/hooks/youtube \
     -d 'hub.topic=https://www.youtube.com/xml/feeds/videos.xml?channel_id=CHANNEL_ID' \
     -d hub.secret=<webhook-secret> \
     https://pubsubhubbub.appspot.com/subscribe
   ```

   `hub.secret` must be the same value stored in the `YouTubeWebhookSecret`
   Secrets Manager secret (the `YOUTUBE_WEBHOOK_SECRET_ID` the endpoint
   verifies deliveries against). Omit it only when no webhook secret is
   configured — with a secret configured, unsigned deliveries are rejected
   with 401.

The hub verifies the callback with a GET `hub.challenge` request before
confirming a subscription; Dapier answers it. Subscriptions lapse when not
renewed, which is what the five-day schedule does. Pull a sample payload with
`dapier triggers sample youtube` (the connected channel's newest upload, or a
documented notification when none exists).

## YouTube upload video

The **Upload video** action (`youtube_upload_video`) posts one video to the
connected channel through the Data API's multipart `videos.insert`. The bytes
come from exactly one of `source_url` (a download URL), a staged
`source_s3: {bucket, key}` object (e.g. `dropbox_read_file`'s output), or
inline `content`. Metadata: `title` (required), optional `description`,
`tags` (comma-separated), and `category_id` (a YouTube category id — 22 is
People & Blogs; YouTube validates it). `privacy_status` defaults to
**unlisted** (like Zapier's Upload Video); `public` and `private` are the
other choices. Output: `{video_id, title, privacy_status, upload_status,
url}`, with `url` the `https://www.youtube.com/watch?v=<id>` watch link.

Scope caveat: **uploading needs the `youtube.upload` scope
(`https://www.googleapis.com/auth/youtube.upload`), and Dapier connections
request only `youtube.readonly` today.** A read-only token fails the upload
with the API's HTTP 403. Enabling the action end to end means adding
`youtube.upload` to the Google Cloud project's scope table (the project
currently lists only `youtube.readonly`, and scope additions need the
project owner's approval — see the governance note under *Current
configuration*), adding the scope to the Dapier YouTube connection with
`dapier connections edit`, and reconnecting the channel so consent covers
it.

The upload stages the whole video in memory (the Lambda has no scratch
disk), so sources should stay modest: Dapier rejects anything larger than
100 MB with a clear error rather than exhausting the run.

## YouTube playlists

The **Create playlist** action (`youtube_create_playlist`) opens an empty
playlist on the connected channel through the Data API's `playlists.insert`
(`part=snippet,status`): `title` (required), optional `description`, and
`privacy_status` — **private** by default (like Zapier's Create Playlist);
`public` and `unlisted` are the other choices. Output: `{playlist_id, title,
url}`, with `url` the `https://www.youtube.com/playlist?list=<id>` link; the
id chains into Add to playlist.

The **Remove from playlist** action (`youtube_remove_from_playlist`) takes
one item back out through `playlistItems.delete`. It addresses the *playlist
item*, not the video: the `playlist_item_id` comes from Add to playlist's
output or a Find Playlist Videos listing. An item already gone (HTTP 404) is
`{removed: false}`, not an error.

Scope caveat: **both playlist actions need the `youtube.force-ssl` scope
(`https://www.googleapis.com/auth/youtube.force-ssl`) — or full `youtube` —
and Dapier connections request only `youtube.readonly` today** (the same
table that gates `youtube.upload` above). A read-only token fails with the
API's HTTP 403; enabling them end to end follows the same steps: add
`youtube.force-ssl` to the project's scope table, add it to the Dapier
YouTube connection with `dapier connections edit`, and reconnect.

## Google OAuth publishing status

On 2026-09-27, the project owner authorized and moved `dtcdev-click` from
**Testing** to **In production**. Both Workspace accounts reconsented after
publication so their tokens were issued under the production status. Google's
OAuth documentation says refresh tokens issued while an External app is in
Testing expire after seven days; production tokens generally avoid that limit,
though they can still be revoked or expire after prolonged inactivity. See
Google's [OAuth refresh-token documentation](https://developers.google.com/identity/protocols/oauth2).

The following public URLs are implemented and saved in **Google Auth Platform
→ Branding**:

| Branding field | URL | Verification |
|----------------|-----|--------------|
| Application home page | `https://dapier.dtcdev.click/about` | Public static page; unauthenticated GET returned HTTP 200 without redirect |
| Application privacy policy | `https://dapier.dtcdev.click/privacy` | Public static page; unauthenticated GET returned HTTP 200 without redirect |
| Application Terms of Service | `https://dapier.dtcdev.click/terms` | Public static page; unauthenticated GET returned HTTP 200 without redirect |

The pages are public routes before authentication in
[`src/dapier/api/router.py`](../../src/dapier/api/router.py); their API Gateway
GET routes are in [`template.yaml`](../../template.yaml). The root `/` is the
authenticated console, so use `/about` as the OAuth homepage. The domain
`dtcdev.click` is authorized. These URLs were deployed on `main` and verified
directly without browser credentials. Deploying changes to them goes through
the normal GitHub Actions deployment.

No logo was added. The app is **In production but unverified**: Google still
shows the unverified-app warning, and the project reports a 100-user lifetime
cap for unapproved sensitive or restricted scopes. `drive.readonly` is
Restricted, so Google verification and possibly a security assessment may be
needed for broader use. The project status change removes the Testing-mode
refresh-token expiry for newly issued tokens; it does not remove the warning
or the cap.

### Moving the project out of Testing

Publishing is a project-wide change: it also affects `DTC DEV Auth` and any
other OAuth client in `dtcdev-click`. Google recommends separating test and
production projects. The `drive.readonly` scope is Restricted and may require
verification and a security assessment depending on app use. Unverified
sensitive or restricted scopes can show users a warning and enforce a
100-new-user cap. Test-user status does not remove those production limits.

The project-wide production change was completed on 2026-09-27: open **Google
Auth Platform → Audience → Publish app**, review the warning that any Google
Account can access the app, click **Confirm**, and verify Audience shows **In
production**. This affects every OAuth client in `dtcdev-click`, including
`DTC DEV Auth`. The app still needs Google verification; publishing is not the
same as verification. See Google's
[production readiness overview](https://developers.google.com/identity/protocols/oauth2/production-readiness/overview)
and [restricted-scope verification requirements](https://developers.google.com/identity/protocols/oauth2/restricted-scope-verification).

## Lessons from the setup

- Use **Google Auth Platform** navigation. The old **APIs & Services → OAuth
  consent screen** link may land on Auth Platform Overview; use **Audience**,
  **Data access**, **Clients**, and **Branding** in its menu.
- Confirm `dtcdev-click` in the project picker before changing anything. The
  consent branding and audience are shared with other OAuth clients.
- Enable APIs before editing scopes. Saving scopes required **Add to table →
  Update → Save**. Adding a tester required pressing **Enter** to commit the
  address chip before **Save**.
- A Google console section can show a blank content pane while loading. Wait a
  few seconds before retrying navigation; this was a transient load state.
- The project-wide scope table only allows the app to request scopes. Also
  update the Dapier connection's requested scopes, reconnect the account, and
  inspect its actually granted scopes. These three lists/states are distinct.
- Avoid altering the shared `DTC` branding or `DTC DEV Auth` client. Google
  production status, by contrast, applies project-wide and affects both.
- Public information pages do not require authentication. Verify each URL
  with unauthenticated GET before entering it in Branding. The root console
  route is not the OAuth homepage.
- When a previous scope-add operation appears unsaved, return to Data access
  and verify the persisted scope table before trying to add them again. The
  working save sequence was **Add to table → Update → Save**.
- On Google's OAuth consent summary, the requested Drive, Docs, and Sheets
  permission checkboxes were initially unchecked. Click **Select all** and
  verify every requested permission is checked before **Continue**. Partial
  consent returns Dapier to Connections with `oauth=missing_scopes`; retry the
  consent and select all requested scopes.
- Google shows **Advanced → Go to dtcdev.click (unsafe)** for this production
  app until verification is complete. This is Google's unverified-app
  warning, not a Dapier login page. Continue only for the approved Dapier
  client and the intended account.
- If `dapier connections connect` returns immediately for an already
  connected connection, confirm the actual account and `granted_scopes`.
  Complete the generated authorization URL in Chrome when the scopes still
  need consent; status alone does not prove the updated grants were accepted.
- Reauthorize existing accounts after switching from Testing to In production
  so their refresh tokens are issued under the production status.

For where to change default scopes in code and the common CLI workflow, use the
[shared connector guide](../../docs/connectors/README.md#where-scope-behavior-lives-in-the-repository).

---

## Google Calendar connector

The Google Calendar connector covers Zapier's Calendar staples: create an
event (detailed or quick-add), find events (with a find-or-create option),
update one, delete one, and a "New Event" poll trigger. Everything runs
through a Google OAuth connection — the same connection type Drive and
Sheets use — talking to the Calendar API v3.

The connector is registered as `google-calendar` (palette chip, discovery
sources, poll source). Connection ids are separate from the connector name:
the docs' scheduling account is connected as `google-calendar` (see
[the Google guide](#the-two-workspace-account-connections)).

### Connect it

The Google connector uses the shared Google OAuth client
([google.md) documents the client setup). Create or update the
connection with the CLI:

```sh
uv run dapier connections create google-calendar --provider google --scopes \
  https://www.googleapis.com/auth/calendar.freebusy \
  https://www.googleapis.com/auth/calendar.events.owned \
  https://www.googleapis.com/auth/calendar.readonly \
  https://www.googleapis.com/auth/userinfo.email
uv run dapier connections connect google-calendar --agent <agent-name>
```

The console's Google connection card requests exactly these scopes.

| Scope | Purpose | Google classification |
|-------|---------|-----------------------|
| `https://www.googleapis.com/auth/calendar.events.owned` | Create, update, and delete events on calendars the user owns | Sensitive |
| `https://www.googleapis.com/auth/calendar.readonly` | The calendars/events listings and the New Event poll | Sensitive |
| `https://www.googleapis.com/auth/calendar.freebusy` | Free/busy lookups for the scheduling flows | Sensitive |
| `https://www.googleapis.com/auth/userinfo.email` | Verify the consenting account at connect time | Basic identity |

#### Consent-time scope step (ops)

The live Google OAuth client must request the calendar scopes at consent
time. Add each scope from the table above to the Google Auth Platform
project's **Data Access** scope list (the procedure and the project's
current scope table are in [google.md)), keep the Dapier
connection's scope list in sync, then reconnect the account — changing a
scope list never updates an already issued refresh token. Dapier itself adds
no code-level gate on top: a Google connection verifies through the
`userinfo.email` identity check, so a connection granted the calendar scopes
verifies and serves the connector as-is.

### Actions

| Action | Zapier counterpart | Notes |
|--------|--------------------|-------|
| `calendar_create_event` | Create Detailed Event | Full event: title, start/end, timezone, description, location, attendees. |
| `calendar_quick_add` | Quick Add Event | One line of text ("Reviewer call tomorrow 10am"), Calendar parses it. |
| `calendar_find_events` | Find or Create Event | Text `q` over a time window (defaults: yesterday through the next quarter); with **Create if missing** a miss posts the event instead of returning `found: false`. |
| `calendar_update_event` | Update Event | Patches only the fields that are set; attendees replaces the whole list. |
| `calendar_delete_event` | Delete Event | Removes one event; pair with a find step when the event may already be gone. |

Times are ISO: a bare `YYYY-MM-DD` is an all-day event (Google's `date`
field), anything else a `dateTime` with the optional IANA `timezone` pinned
on. Start and end must agree on the style — Google rejects mixed event
times. Find outputs chain into update/delete through
`{steps.<id>.output.event.event_id}`.

### Trigger: New Event (poll)

`event.new` is a poll, not a webhook: Calendar has no event-creation push,
so a stored poll trigger lists the calendar on the poll schedule
(`google-calendar.events` source, `rate(5 minutes)` is a good cadence) and
fires one event per **newly created** event.

The cursor is the event's `created` timestamp, seeded on the first fire so
enabling a trigger does not fire the calendar's whole history; an edit to an
old event never poses as a new event. A recurring series' occurrences carry
the series' creation time, so a new week of a weekly series does not fire
one event per occurrence.

Save a poll trigger from a JSON file:

```sh
uv run dapier triggers polls save calendar-news.json
```

```json
{
  "name": "calendar-news",
  "expression": "rate(5 minutes)",
  "source": "google-calendar.events",
  "calendar_id": "ops@example.test",
  "connection_id": "google-calendar",
  "actions": [{"type": "email_send", "to": "ops@example.test"}]
}
```

`connection_id` is required (the poll refreshes the connection's OAuth
token); `calendar_id` takes any id from the calendars listing, `primary`
included.

### Discovery

The connection's listings feed the field pickers (console and CLI):

| Resource | Lists |
|----------|-------|
| `google-calendar.calendars` | Calendars the connection can see, with timezones — the actions' Calendar ID field browses it. |
| `google-calendar.events` | Upcoming events in one calendar (`calendar_id` required, optional text `query`). |

```sh
uv run dapier connections discover google-calendar calendars
uv run dapier connections discover google-calendar events --param calendar_id=ops@example.test
```

The trigger config offers the same listings as options (a calendar picker,
and per-calendar event options).

---

## YouTube connector

YouTube is a Google OAuth connection (provider `youtube`) on the shared
Google client. The [Google guide) is the source of truth for:

- creating and connecting the channel — OAuth consent with
  `youtube.readonly`, channel verification, and the Brand Account fallback
  when no channel is found ([Google setup](#google-cloud-scope-configuration)
  and [account connections](#calendar-youtube-and-other-accounts));
- the `video.published` push trigger — YouTube's PubSubHubbub feed on the
  shared `/hooks/youtube` callback, the hub secret, and the five-day
  renewal schedule ([YouTube video push](#youtube-video-push-websub));
- the **Upload video** action and its scope caveat: uploading needs
  `https://www.googleapis.com/auth/youtube.upload`, and Dapier connections
  request only `youtube.readonly` today — a read-only token fails the
  upload with HTTP 403 ([YouTube upload video](#youtube-upload-video)).

This page is the YouTube-specific rest: triggers without webhooks, the
action catalog, and discovery.

### Triggers

| Trigger | Kind | Notes |
| --- | --- | --- |
| `youtube` / `video.published` | hook (push) | fires from the PubSubHubbub notification; setup lives in the [Google guide](#youtube-video-push-websub). The trigger stores a `channel_id` filter; disabling or deleting never unsubscribes — the shared callback means other watchers keep the subscription, and the renewal schedule releases released channels by lapsing. |
| `youtube.videos` poll | poll (no webhook) | the no-push path: a stored poll trigger lists the watched channel's uploads playlist on the schedule and publishes the same `youtube` / `video.published` event, so workflows match the same chip either way |

The poll trigger (`dapier polls save`) needs the `connection_id` of the
YouTube connection to poll as (its OAuth token is refreshed like the
actions' calls); an empty `channel_id` resolves to the connection's own
channel at fetch time:

```json
{
  "name": "channel-uploads",
  "source": "youtube.videos",
  "expression": "rate(1 hour)",
  "connection_id": "youtube",
  "flow": "channel-uploads"
}
```

Each fire lists recent uploads (the channel id with `UC` swapped for `UU`
— the uploads playlist) and publishes videos published strictly after the
stored watermark, in the notification's data shape: `{id, video_id,
channel_id, title, url, published}`. `published` rides along only from the
poll — the webhook notification itself carries no publish time. The first
fire seeds the watermark without emitting.

### Actions

All actions take `connection_id`.

| action | YouTube Data API | notes |
| --- | --- | --- |
| `youtube_find_video` | `search.list` | top videos for a search query |
| `youtube_find_playlist_items` | `playlistItems.list` | one playlist's videos, newest first; `playlist_id` from the playlists discovery |
| `youtube_upload_video` | `videos.insert` (multipart) | needs the `youtube.upload` scope ([caveat](#youtube-upload-video)); bytes from exactly one of `source_url`, staged `source_s3 {bucket, key}`, or inline `content`; `privacy_status` defaults to unlisted; ~100 MB ceiling |
| `youtube_add_to_playlist` | `playlistItems.insert` | adds one video to an editable playlist; an already-present video surfaces YouTube's `videoAlreadyInPlaylist` error rather than duplicating silently |
| `youtube_update_video` | `videos.update` (snippet) | title required — YouTube replaces the whole snippet, so pass `category_id` (and the description, which an omitted value clears) when they matter |

### Discovery

YouTube connections list live resources over
`dapier connections discover <connection> [resource]` and the console/CLI
pickers: `channel` (the connected channel), `playlists`, `playlist_items`
(one playlist's videos, needs `playlist_id`), and `videos` (the channel's
recent uploads, newest first).

### Gotchas

- Connection setup, scopes, and the WebSub subscription lifecycle are
  [Google-guide) territory — this page deliberately does not
  repeat them.
- The push and poll paths publish the same event name; a workflow filter on
  `channel_id` works for both, but only the poll envelope carries
  `published`.
- A youtube poll without a stored `channel_id` resolves the connection's
  own channel through `channels().mine` at fetch time — point it at a
  different channel explicitly when the connection can see more than one.

### Read-only WebSub lease diagnostic

The `youtube_subscription_status` action takes `channel_id` and reads the
[hub's subscriber diagnostic](https://pubsubhubbub.appspot.com/subscribe) using
the deployed callback and existing server-held WebSub secret. It returns only
`active`, `state`, `expires_at`, `topic`, and `callback`; it never returns the
secret-bearing diagnostic URL or raw response. It neither renews nor changes
subscriptions. The action is available in the console designer and through
`dapier workflows test --execute`/the shared workflow-test API. A diagnostic
failure or unrecognized lease fields cannot be reported as an active lease.

`youtube_subscription_renew` accepts `channel_id` only for a channel watched by
an enabled workflow. It renews using the same callback, topic and server-held
secret as the scheduled renewal. This action is available through the shared
designer API, Console action catalog and CLI workflow test path. It sends no
video or Slack notification.

Renewal runs every four days, leaving one day of margin against the observed
five-day hub lease. The renewal Lambda reads the four live workflow/trigger
tables with scoped read-only permissions.
