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
supports authorized API access, but does not itself create a recording trigger
or add a Zoom workflow action.

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
