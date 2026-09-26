# Zoom cloud recording trigger

Zoom does not use Dapier's OAuth client. It sends signed webhooks with the
Secret Token from a Zoom **Webhook Only** app.

1. Enable cloud recording for the Zoom host. In the Zoom App Marketplace,
   create a **Webhook Only** app with an event subscription for
   `recording.completed`.
2. In Dapier **Connections → Zoom → Add account**, paste the app's **Secret
   Token**. Dapier stores it write-only and displays the connection endpoint:
   `https://dapier.dtcdev.click/hooks/zoom/<connection-id>`.
3. Paste the endpoint into the Zoom **Event Notification Endpoint URL** and
   click **Validate**. Dapier answers Zoom's signed challenge. Save the event
   subscription; the connection should show **connected**.
4. Create a workflow with `connector: zoom` and
   `event: recording.completed`, or select **Zoom → recording.completed** in
   the designer.

Dapier emits one event per completed cloud recording containing at least one
MP4 or M4V file. The event includes `connection_id`, `account_id`,
`meeting_id`, `meeting_uuid`, `topic`, `host_email`, `share_url`, and
`video_files` metadata. Download tokens are not placed in workflow events.

The same setup is available in the CLI:

```sh
uv run dapier connections import zoom --provider zoom --token-file /path/to/zoom-secret
```

The CLI prints the endpoint URL; use `--display-name` to name additional Zoom
apps. Replacing the secret returns the connection to **setup incomplete**
until Zoom validates it again. Dapier validates each webhook signature and
rejects requests more than five minutes from their signed timestamp. See
[Zoom's webhook setup and validation guide](https://developers.zoom.us/docs/api/webhooks/).
