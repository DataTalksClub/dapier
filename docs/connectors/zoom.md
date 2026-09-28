# Zoom OAuth and recording webhooks

Dapier has two distinct Zoom connection types:

- **Zoom API OAuth** gives an agent an OAuth token for Zoom APIs. It uses the
  shared Zoom OAuth client and a user consent flow.
- **Recording webhook** lets Dapier receive signed `recording.completed`
  events. It uses a Zoom webhook Secret Token, not an OAuth access token.

Keep these credentials and connection IDs separate. The examples below use
`zoom-api` for an OAuth account and `zoom` for the recording webhook.

## Zoom OAuth for API access

The Zoom OAuth client is separate from the Google and Dropbox clients. On
2026-09-27, `uv run dapier oauth-clients list` reported Zoom as **not
configured**. The Zoom OAuth app and client credentials still need to be
created and saved before connecting an account. Recheck the status before
starting; an operator may have configured it since then.

### Create and configure the OAuth app

1. In the [Zoom App Marketplace](https://marketplace.zoom.us/), sign in with an
   account that has developer permissions. Choose **Develop → Build an app →
   General app**. Select a **User-managed** app so consent is granted by the
   individual user; this is the user-based authorization flow Dapier uses.
   Do not choose Server-to-Server OAuth for this connection.
2. In **Basic Info → OAuth Information**, set both the **OAuth Redirect URL**
   and **OAuth Allow List** to exactly:

   ```
   https://dapier.dtcdev.click/oauth/callback
   ```

   Save the page. Dapier uses the same callback for Google, Dropbox, and Zoom.
   Zoom generates separate Development and Production credentials; use the
   matching redirect setting for the credentials in use. For internal testing,
   use Development credentials. Set up the production redirect entry before
   switching Dapier to Production credentials.
3. On the app's **Scopes** page, add `user:read:user`. Dapier calls
   `GET https://api.zoom.us/v2/users/me` to verify the authorized account; this
   scope is the minimum identity scope used by the adapter. Only add further
   Zoom API scopes when an actual action needs them, and add each scope both
   to the app and to the Dapier connection. Zoom's
   [Get a user API reference](https://developers.zoom.us/docs/api/users/)
   lists the `user:read:user` granular scope for that endpoint.
4. Copy the app's **Client ID** and **Client Secret**. Save them with the
   shared [OAuth credential update procedure](README.md#updating-or-rotating-an-oauth-client-id-and-secret),
   using provider `zoom`. Check `uv run dapier oauth-clients list` afterward;
   it should report Zoom as configured. Never put the secret in a command
   argument or this guide.

Zoom allows a newly generated client secret to overlap with the old one for
30 days. After rotating it in the app credentials page, update Dapier promptly
with the new client ID/secret using the shared procedure. See Zoom's
[app credentials guide](https://developers.zoom.us/docs/build-flow/basic-info/app-credentials/).

Zoom's official guides: [create a user OAuth app](https://developers.zoom.us/docs/integrations/create/),
[user authorization](https://developers.zoom.us/docs/integrations/end-user-auth/),
and [using Zoom APIs](https://developers.zoom.us/docs/api/using-zoom-apis/).

### Create and connect the OAuth account

Use the authenticated `dapier` CLI to create the OAuth connection and open
Zoom consent:

```sh
uv run dapier connections create zoom-api --provider zoom --scopes user:read:user
uv run dapier connections connect zoom-api --agent <agent-name>
```

Check the Zoom account on the consent screen before approving. Dapier verifies
the Zoom user ID and display name, then stores the OAuth access and refresh
tokens write-only. Inspect `uv run dapier connections show zoom-api` to check
the verified account and granted scopes.

The console's Zoom connection card currently configures recording webhooks;
use the CLI flow above for a Zoom API OAuth connection. This OAuth token
supports authorized API access and drives the workflow actions
(`zoom_find_meeting`, `zoom_find_recording`, `zoom_create_meeting`,
`zoom_update_meeting`, `zoom_delete_meeting`, `zoom_add_registrant`,
`zoom_list_past_participants`, and the webinar set `zoom_create_webinar`,
`zoom_find_webinar`, `zoom_update_webinar`, `zoom_add_webinar_registrant`,
`zoom_delete_webinar`, `zoom_list_past_webinar_participants`)
plus the scheduled recording trigger below.

When changing OAuth scopes, first add them in the Zoom app's **Scopes** page,
then update the connection and reconnect:

```sh
uv run dapier connections scopes zoom-api --scopes user:read:user <additional-scope>
uv run dapier connections connect zoom-api --agent <agent-name>
```

For scope changes shared across providers and the repository locations that
define defaults, see [the connector scope guide](README.md#changing-requested-scopes).

## Cloud recording webhook

This is the Zoom integration currently exposed by the Dapier console's Zoom
card and the `zoom` workflow trigger. It does not use the Zoom OAuth client.

1. Enable cloud recording for the Zoom host. In the Zoom App Marketplace,
   create a **Webhook Only** app with an event subscription for
   `recording.completed`.
2. In Dapier **Connections → Zoom → Add account**, paste the app's **Secret
   Token**. Dapier stores it write-only and displays the endpoint URL:
   `https://dapier.dtcdev.click/hooks/zoom/<connection-id>`.
3. Paste the endpoint into the Zoom **Event Notification Endpoint URL** and
   click **Validate**. Dapier answers Zoom's signed challenge. Save the event
   subscription; the connection becomes **connected** after Zoom validates
   it.
4. Create a workflow with `connector: zoom` and
   `event: recording.completed`, or choose **Zoom → recording.completed** in
   the designer.

Dapier emits one event for each completed cloud recording that contains at
least one MP4 or M4V file. The event includes `connection_id`, `account_id`,
`meeting_id`, `meeting_uuid`, `topic`, `host_email`, `share_url`, and
`video_files` metadata. Download tokens are not included in workflow events.

The CLI can provision the webhook connection too:

```sh
uv run dapier connections import zoom --provider zoom --token-file /path/to/zoom-webhook-secret
```

Use `--display-name` to name another Zoom recording app. The CLI prints the
endpoint URL. Replacing the webhook secret returns the connection to setup
incomplete until Zoom validates it again. Dapier verifies every webhook
signature and rejects requests more than five minutes from their signed
timestamp. See [Zoom's webhook setup and validation guide](https://developers.zoom.us/docs/api/webhooks/).

For OAuth accounts, Dapier also lists upcoming meetings for field pickers
(`dapier connections discover <connection> meetings`, and
`dapier triggers sample zoom --resource zoom.meetings`); the
`zoom_find_meeting` action's Meeting ID field browses the same listing.

### Browse meetings, past meetings, webinars and recordings

Dapier lists upcoming meetings for field pickers
(`dapier connections discover zoom-api meetings`, or
`dapier triggers sample zoom --resource zoom.meetings`), the last 30
days of cloud recordings (`recordings`), meetings already held
(`past_meetings` — the `zoom_list_past_participants` and
`zoom_delete_meeting` fields browse it), and upcoming webinars
(`webinars`); the `zoom_find_meeting` action's Meeting ID field browses the
same listing in the designer. Topic searches that need the past window use
the `scope` field: `zoom_find_meeting` and `zoom_find_webinar` take
`scope: upcoming` (default) or `scope: past`, which switches the listing
between Zoom's `upcoming` and `previous_meetings` windows.

## New recordings on a schedule (poll trigger)

The webhook section above needs a Zoom app: a Webhook Only app whose
endpoint Dapier validates. The `zoom.recordings` poll source is the
no-app-config path to the same `recording.completed` event: a stored poll
trigger lists the connected account's cloud recordings on a schedule and
publishes `zoom`/`recording.completed` events, so a workflow matches the
same Zoom chip either way. It uses the Zoom API OAuth connection (the
`zoom-api` flow above), not the webhook-only connection.

The poll reads recordings, which needs Zoom's recordings read scope on the
app and the connection (see the scope guidance above — app first, then the
connection):

```sh
uv run dapier connections scopes zoom-api --scopes user:read:user recording:read:recording
uv run dapier connections connect zoom-api --agent <agent-name>
```

Save the trigger through the API, the console's Triggers view, or the CLI.
`dapier polls save` takes a JSON body; the Zoom bits are `source`,
`connection_id`, and the optional `for_email`:

```json
{
  "name": "zoom-recordings",
  "expression": "rate(15 minutes)",
  "source": "zoom.recordings",
  "connection_id": "zoom-api",
  "for_email": "me",
  "actions": [{"type": "email_send", "to": "team@example.test"}]
}
```

`for_email` defaults to `me` — the connected user's own recordings; pass a
host's email to poll their mailbox instead (Zoom lists what the authorized
user may see). Behavior, mirroring the webhook trigger:

- Each fire lists the last 30 days of recordings through
  `GET /users/{for_email}/recordings` with the connection's refreshed OAuth
  token; only recordings that carry at least one video file (MP4 or M4V)
  fire, like the webhook path.
- The first fire after enabling seeds its watermark and emits nothing —
  the account's existing recordings are history, not news. After that, only
  recordings newer than the last fired one fire, oldest first, deduped by
  the trigger's seen-set across the overlapping lookback windows.
- One event per recording. Its data matches the webhook payload shape —
  `meeting_id`, `meeting_uuid`, `topic`, `host_id`, `start_time`,
  `share_url`, `video_files` — plus a top-level `download_url` of the first
  video file, so downstream steps template the poll and webhook fires
  identically.

List what a stored poll watches with `dapier polls list` (the Watches column
shows `zoom.recordings <for_email>`), and pull a sample payload with
`dapier triggers sample zoom --event zoom-recordings`.

## Archive recordings to S3

Recording events and `zoom_find_recording` both carry the recording's
`download_url`, and `s3_upload` fetches a `source_url` directly — so
recording → archive needs no downloader step in between:

1. Trigger on `zoom.recordings` (or the recording.completed webhook); the
   event data carries `download_url` and `meeting_id`.
2. Add an `s3_upload` step: `bucket` and `key` (for example
   `zoom/{meeting_id}.mp4`), `source_url: {download_url}`, and
   `source_connection_id: zoom` — the upload fetches the URL with the
   zoom connection's OAuth token (refreshed automatically), which is what
   Zoom's authenticated download links require. The AWS side rides the
   shared `aws` credential via `credential_id`.

`source_url` is templated like every field, so a `zoom_find_recording`
step (by meeting id, topic, or latest) can feed the same upload for
backfills of recordings that predate the trigger.

## Create meetings

`zoom_create_meeting` (Zapier's "Create Meeting") creates one meeting on the
connected user's account: scheduled when Start time renders from the event
(an ISO 8601 datetime such as `2026-10-01T09:00:00Z`), instant when it is
empty. The step output carries the meeting's `id`, `join_url`, `start_url`,
`passcode`, `start_time`, and `duration`, so later steps can email the
invite or post it to Slack.

Creating needs write access the find actions don't. Before using the action:

1. Add the `meeting:write:meeting` granular scope on the Zoom app's
   **Scopes** page (see the scope guidance above — scopes are added to the
   app first, then to the connection).
2. Update the connection with the full scope list and reconnect:

   ```sh
   uv run dapier connections scopes zoom-api --scopes user:read:user meeting:write:meeting
   uv run dapier connections connect zoom-api --agent <agent-name>
   ```

The Settings field takes a Zoom meeting-settings JSON object (for example
`join_before_host`, `waiting_room`); `{tokens}` expand inside it and literal
braces double, exactly like Mailchimp merge fields.

`zoom_find_meeting` can reach the same write path as find-or-create (Zapier's
"Find or Create Meeting"): with **Create if missing** enabled, a topic search
that finds nothing creates the meeting instead — Topic names the created
meeting, and Start time, Duration, Time zone, Agenda, and Settings (the same
fields as `zoom_create_meeting`, all optional) shape it. The output reports
`found: false` with `created: true` and the created meeting's `id`,
`join_url`, and friends, so one step serves "find the standup or spin it
up". A search that hits never creates, and a lookup by Meeting ID never
creates — a dead id is an explicit reference, not a name to reserve.
Creating needs the `meeting:write:meeting` scope above; find-only use does
not.

## Update and delete meetings

`zoom_update_meeting` (Zapier's "Update Meeting") reschedules or renames one
meeting (`PATCH /meetings/{id}`). Only the fields that render non-empty are
sent — Topic, Start time, Duration, Time zone, Agenda, Settings — so the
rest of the meeting stays untouched; sending none of them is a save-time
error, not an empty patch. Field rules mirror Create: Start time is an ISO
8601 datetime, Duration is whole minutes. Zoom answers with no body, so the
step output carries `updated`, `meeting_id`, and `updated_fields` (the
fields that were sent).

`zoom_delete_meeting` removes one meeting (`DELETE /meetings/{id}`) and
outputs `deleted: true` with the `meeting_id`. For a recurring meeting the
whole series is deleted unless Occurrence ID scopes the delete to a single
occurrence — leave it empty to retire the series deliberately. A meeting id
that no longer exists fails the step with Zoom's HTTP 404; when the meeting
may already be gone, branch on `zoom_find_meeting`'s `found` output first.

Update and delete need the same write access as creating: the
`meeting:write:meeting` granular scope on the app and the connection (see
the scope steps under *Create meetings* above).

## Webinars

The webinar actions mirror the meeting set one level up on Zoom's REST API:

- `zoom_create_webinar` (Create Webinar) creates one webinar on the
  connected account: scheduled (Zoom type 5) when Start time renders from
  the event, recurring with no fixed time (Zoom type 6) when it is empty.
  The field set is `zoom_create_meeting`'s — Topic, Start time, Duration,
  Time zone, Agenda, Settings — and the step output carries the webinar's
  `id`, `join_url`, `start_url`, and `passcode`.
- `zoom_find_webinar` (Find Webinar) looks one webinar up by id or by topic
  across the `scope` window — `upcoming` (default) or `past`; a miss is
  `found: false`, not an error.
- `zoom_update_webinar` (Update Webinar) reschedules or renames one webinar
  (`PATCH /webinars/{id}`), sending only the fields that render non-empty;
  updating nothing is a save-time error.
- `zoom_add_webinar_registrant` seats one person on a webinar
  (`POST /webinars/{id}/registrants`) and outputs their unique `join_url`,
  like `zoom_add_registrant` for meetings.
- `zoom_delete_webinar` removes one webinar (`DELETE /webinars/{id}`); on a
  recurring webinar, `occurrence_id` scopes the delete to one occurrence,
  like `zoom_delete_meeting`.
- `zoom_list_past_webinar_participants` lists who attended one past webinar
  (`GET /past_webinars/{id}/participants`) — pair it with the
  `webinar.ended` trigger for attendance emails and sheet rows, exactly
  like `zoom_list_past_participants` for meetings.

The Webinar ID fields browse the `webinars` discovery listing
(`dapier connections discover zoom-api webinars`, or
`dapier triggers sample zoom --resource zoom.webinars`).

Webinars need the webinar scopes alongside `user:read:user`: the read side
(`zoom_find_webinar`, the past-participants listing, the pickers) works with
`webinar:read:admin` or the
user-level granular `webinar:read:webinar`; creating, updating, deleting and
adding registrants need `webinar:write:admin` (or the user-level granular
`webinar:write:webinar`, plus `webinar:write:registrant` for the
registrant endpoint). As with meetings, add each scope to the Zoom app's
**Scopes** page first, then to the connection:

```sh
uv run dapier connections scopes zoom-api --scopes user:read:user webinar:read:admin webinar:write:admin
uv run dapier connections connect zoom-api --agent <agent-name>
```

The Zoom chip also carries the webinar trigger events
(`webinar.started`, `webinar.ended`, `webinar.registration_created`): the
webhook intake maps them with the same builders as the meeting events, and
each publishes the same metadata-only payload under its own event name —
subscribe to them on the Webhook Only app's event subscription like the
recording events above.
