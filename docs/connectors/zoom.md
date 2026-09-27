# Zoom meetings and cloud recordings

Dapier uses two separate Zoom connections:

- `zoom-api` is a user-authorized OAuth connection for Zoom REST API requests.
- `zoom` is a webhook connection for Zoom cloud-recording notifications.

The OAuth secret and the webhook Secret Token are different credentials. Keep
them in their respective connections.

## OAuth app and requested scopes

The current app is **Dapier Zoom**, a **User-managed General app** in the Zoom
App Marketplace. User-managed consent limits this app to the Zoom account that
authorizes it. The app uses Development credentials for this personal setup;
publishing to the Zoom Marketplace is not needed for local account use.

Set the Development **OAuth Redirect URL** to the exact Dapier callback below
and leave **Use Strict Mode for Redirect URLs** enabled:

```
https://dapier.dtcdev.click/oauth/callback
```

The callback must also appear in the app's OAuth Allow List. The scopes in the
app and in `zoom-api` are:

| Scope | Purpose |
|---|---|
| `user:read:user` | Verify which Zoom account authorized the connection. |
| `meeting:read:list_meetings` | List the user's meetings. |
| `meeting:read:meeting` | Read a meeting and its details. |
| `meeting:write:meeting` | Create meetings. |
| `meeting:update:meeting` | Change meeting details, including its scheduled time. |
| `cloud_recording:read:recording` | Receive and read completed cloud-recording event details. |

These scopes cover account verification, meeting creation/rescheduling, and
the existing `recording.completed` trigger. The recording event includes
`meeting_id`, `meeting_uuid`, `topic`, `share_url`, and video-file metadata,
so a workflow can associate a recording with its Zoom meeting. Dapier does not
currently have separate Zoom-specific create/update actions; use the generic
**HTTP request** workflow action with `connection_id: zoom-api` for Zoom API
calls. The recording trigger itself is already implemented.

Zoom documents the [OAuth and PKCE flow](https://developers.zoom.us/docs/integrations/oauth/),
[granular scopes](https://developers.zoom.us/docs/integrations/oauth-scopes-granular/),
[meeting endpoints](https://developers.zoom.us/docs/api/meetings/), and
[webhook event setup](https://developers.zoom.us/docs/api/webhooks/).

### Configure the OAuth client

1. In Zoom Marketplace, open **Dapier Zoom → Basic Information → App
   Credentials → Development**. Use the app's **Client ID** and matching
   **Client Secret**. Do not use the webhook Secret Token from **Features →
   Access** as the OAuth secret.
2. On the app's **Scopes** page, add the six scopes in the table above and
   save.
3. Store the Development client in Dapier. Paste the secret through stdin; do
   not put it in a command argument, shell history, or this guide:

   ```powershell
   $secret = Get-Clipboard -Raw
   if ([string]::IsNullOrWhiteSpace($secret)) { throw 'Copy the Zoom Client Secret first.' }
   try {
     $secret | uv run dapier oauth-clients set zoom --client-id '<Zoom Development Client ID>' --client-secret-file -
     if ($LASTEXITCODE -ne 0) { throw 'Dapier did not store the Zoom OAuth client.' }
   } finally {
     Remove-Variable secret -ErrorAction SilentlyContinue
     Set-Clipboard -Value ''
   }
   uv run dapier oauth-clients list
   ```

   If `uv` is not on `PATH`, invoke the installed executable, for example
   `& "$env:USERPROFILE\.local\bin\uv.exe" run dapier ...`. Verify Zoom is
   **configured**; the command never prints the secret. The saved client is
   live immediately and does not need a redeploy.

### Create and authorize the OAuth connection

The intended connection is named `zoom-api` and is granted to the existing
`personal-scheduler` agent. Create it once; when it already exists, inspect it
with `dapier connections show zoom-api` and update its scopes instead of
creating a duplicate.

The console's Zoom card manages the recording webhook. Use the CLI flow below
to create or reconnect the Zoom API OAuth connection.

```powershell
uv run dapier connections create zoom-api --provider zoom `
  --display-name 'Zoom Meetings and Recordings' `
  --scopes user:read:user meeting:read:list_meetings meeting:read:meeting `
    meeting:write:meeting meeting:update:meeting cloud_recording:read:recording
uv run dapier connections connect zoom-api --agent personal-scheduler
```

Check that the Zoom account shown in Dapier after consent is the intended
account. Dapier verifies the authorized user's Zoom ID and display name. The
agent needs a `use` grant (and `connect` only if it will reconnect the account)
for `zoom-api`; follow the existing `personal-scheduler` grant pattern in
`uv run dapier grants list`.

### Create or move a meeting in a workflow

Use a Dapier **HTTP request** action. It adds a Bearer token from the named
OAuth connection and templates the URL and body. For a user-level Zoom app,
create with `POST https://api.zoom.us/v2/users/me/meetings`. To move an existing
meeting, send `PATCH https://api.zoom.us/v2/meetings/<meeting-id>` with a new
future `start_time` (and `timezone` if needed). For example:

```yaml
- id: create_zoom_meeting
  type: http_request
  method: POST
  url: https://api.zoom.us/v2/users/me/meetings
  connection_id: zoom-api
  body: '{{"topic":"Planning","type":2,"start_time":"{trigger.start_time}","duration":30,"timezone":"Europe/Berlin"}}'

- id: move_zoom_meeting
  type: http_request
  method: PATCH
  url: https://api.zoom.us/v2/meetings/{trigger.meeting_id}
  connection_id: zoom-api
  body: '{{"start_time":"{trigger.new_start_time}","timezone":"Europe/Berlin"}}'
```

The doubled outer braces escape literal JSON braces for Dapier's template
renderer. Replace the example trigger fields with the fields in the workflow
that will supply the meeting time and ID. A move of a recurring meeting may
need recurrence-specific handling. Zoom documents that `start_time` must be
in the future and limits create/update requests; see its [meeting API
reference](https://developers.zoom.us/docs/api/meetings/).

## Recording notifications and meeting links

Use the same **Dapier Zoom** General app; do not create a separate Webhook Only
app. Under **Features → Access**, enable **Event Subscription**, add an event
subscription for `recording.completed`, set its Event Notification Endpoint
URL to the Dapier endpoint, and save/validate it. The endpoint for connection
ID `zoom` is:

```
https://dapier.dtcdev.click/hooks/zoom/zoom
```

The endpoint uses the app's **Secret Token** from the Zoom event-subscription
settings. Import that token separately into Dapier. Copy it to the Windows
clipboard, then use a private temporary file so it never appears in command
arguments or shell history:

```powershell
$secretPath = Join-Path $env:TEMP 'dapier-zoom-webhook-secret.txt'
$secret = Get-Clipboard -Raw
if ([string]::IsNullOrWhiteSpace($secret)) { throw 'Copy the Zoom webhook Secret Token first.' }
try {
  Set-Content -LiteralPath $secretPath -Value $secret -NoNewline
  uv run dapier connections import zoom --provider zoom `
    --token-file $secretPath --display-name 'Zoom recording notifications'
  if ($LASTEXITCODE -ne 0) { throw 'Dapier did not import the Zoom webhook token.' }
} finally {
  Remove-Item -LiteralPath $secretPath -Force -ErrorAction SilentlyContinue
  Remove-Variable secret -ErrorAction SilentlyContinue
  Set-Clipboard -Value ''
}
```

The CLI prints the endpoint URL. Paste that exact URL into the Zoom event
subscription, select **All Recordings have completed** (`recording.completed`),
and save. Zoom's documentation says to click **Validate** beneath the endpoint
URL; its `endpoint.url_validation` challenge marks the Dapier connection
connected. Enable cloud recording on the Zoom host as well.

### Validation status and current Zoom UI behavior

Do not treat recording delivery as ready until
`uv run dapier connections show zoom` reports `status: connected`. The
`zoom-api` OAuth connection can be connected while this separate webhook
connection is still `ready`.

On 2026-09-27, Zoom's current Build Flow **Features → Access** editor showed
only **Save** and **Cancel** for an existing or new event subscription; it did
not show the documented **Validate** action. Saving the existing subscription
again and creating then removing a temporary retry subscription did not send a
validation challenge. The original `Dapier cloud recordings` subscription is
still configured, but its Dapier connection remains `ready`. Do not create
duplicate subscriptions to retry validation. If the current UI still has no
**Validate** control, stop and resolve the Marketplace UI/documentation
mismatch before relying on recording notifications. Confirm the Dapier
webhook Secret Token matches the app's current Secret Token before retrying;
regenerating it invalidates the previous value.

A workflow can listen for the event with:

```yaml
connector: zoom
event: recording.completed
```

The trigger filters out events without an MP4/M4V recording. It sends meeting
IDs (`meeting_id` and `meeting_uuid`), topic, start time, host email, share
URL, and each video's ID, type, size, play URL, and download URL. Those IDs
provide the cross-link to the Zoom meeting; a workflow can also use
`zoom-api` in an HTTP request action to fetch more meeting details. Download
tokens are not included in Dapier run data.

## Scope changes, credential rotation, and recovery

To add or remove a permission, update both sides and ask Zoom for consent
again:

1. Add/remove the scope in **Dapier Zoom → Scopes** and save.
2. Replace the complete connection scope list:

   ```powershell
   uv run dapier connections scopes zoom-api --scopes user:read:user meeting:read:list_meetings meeting:read:meeting meeting:write:meeting meeting:update:meeting cloud_recording:read:recording <additional-scope>
   ```

3. Run `uv run dapier connections connect zoom-api --agent personal-scheduler`
   and approve the updated request. Inspect `dapier connections show
   zoom-api` to confirm account and granted scopes.

To rotate a Zoom OAuth secret, copy the matching Development Client Secret
from **App Credentials**, then rerun the `oauth-clients set zoom` command
above. A configured result confirms storage; reconnect to verify the provider
accepts it. To replace a webhook Secret Token, import it again with
`connections import zoom`; then run Zoom's endpoint validation again.

If Zoom consent returns to Dapier with `oauth=exchange_failed`, check the
OAuth client ID/secret pair and ensure the deployed Dapier Zoom token exchange
sends the PKCE `code_verifier` together with the Basic client-auth header.
Zoom supports PKCE for confidential clients as well as public clients.
