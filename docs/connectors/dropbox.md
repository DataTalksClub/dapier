# Dropbox connector

The Dapier Dropbox client is configured in the Dropbox App Console and saved
with the shared [OAuth client rotation procedure](README.md#updating-or-rotating-an-oauth-client-id-and-secret).
The provider app's allowed permissions and a Dapier connection's requested
scopes are separate; both must include the required access, and an existing
token must reconnect after scope changes.

## Current configuration

- App: **Dapier DTC Dev**.
- App key / OAuth client ID: `aia0r7fjj4ls6j9` (verify it still matches in the
  console before updating Dapier).
- Access type: **Scoped access → Full Dropbox**.
- OAuth callback: `https://dapier.dtcdev.click/oauth/callback`.
- Invoice watcher path: `/_dtc_paperwork/income-invoices/`.
- Required app and connection scopes: `account_info.read`,
  `files.metadata.read`, `files.content.read`, and `files.content.write`.

## App configuration

1. Open the [Dropbox App Console](https://www.dropbox.com/developers/apps) and
   select **Dapier DTC Dev**. If it does not exist, create a **Scoped access**
   app with **Full Dropbox** access. The new-app form requires checking **I
   agree to the Dropbox API Terms and Conditions** before creation. The user
   explicitly approved accepting these terms for this named app and account.
   That approval covers this Dropbox account, app name, Full Dropbox access,
   listed scopes, and callback only; ask again if any of those change.
2. Full Dropbox is required because the enabled `dropbox_on_upload` workflow
   watches and deletes processed files at the root-level path
   `/_dtc_paperwork/income-invoices/`. An App Folder app cannot reach that
   path. Dropbox treats the access type separately from scopes, and changing
   access type requires deleting/recreating an app. Create a separate Full
   Dropbox app if the existing app is App Folder. Use App Folder only after
   moving the workflow's Dropbox paths under that folder.
3. Open **Permissions** and grant `files.metadata.read`, `files.content.read`,
   and `files.content.write`. `account_info.read` is enabled by default and
   may appear checked but disabled. Click **Submit** at the bottom to persist
   permission changes.
4. Open **Settings → OAuth 2 → Redirect URIs**, add
   `https://dapier.dtcdev.click/oauth/callback`, then click **Add** to commit
   it.
5. Copy the **App key**. Click **Show** beside **App secret** and copy the
   current secret. Save both immediately using the shared credential update
   procedure. Never save the secret in this guide.

See Dropbox's [App Console guide](https://docs.dropboxapi.com/dropbox-api/docs/get-started/tutorial/app-console)
and [OAuth content access and scope guide](https://docs.dropboxapi.com/dropbox-api/docs/oauth).

## Connecting the invoice workflow

1. In Dapier **Connectors → Dropbox → Create new**.
2. Approve all four scopes: `account_info.read`, `files.metadata.read`,
   `files.content.read`, and `files.content.write`. Dapier requests offline
   access so it can use the refresh token.
3. Confirm the connection shows **connected** with the expected Dropbox
   account ID. Then confirm the workflow uses the intended root path.

The scope meanings are:

| Scope | Needed for |
|-------|------------|
| `account_info.read` | Verify the Dropbox account identity during connection setup |
| `files.metadata.read` | Watch/list files and inspect metadata |
| `files.content.read` | Download invoice contents |
| `files.content.write` | Upload or delete files; the invoice workflow deletes processed files |

## Setup history and troubleshooting

- The existing client key is `aia0r7fjj4ls6j9`. During the first setup, the
  stored secret did not match the current App Console secret. Copy the current
  App secret from **Dapier DTC Dev → Settings → OAuth 2** and update the
  client with `uv run dapier oauth-clients set dropbox --client-id
  aia0r7fjj4ls6j9 --client-secret-file -`; then retry setup. Follow the
  clipboard-safe example in the [shared credential procedure](README.md#updating-or-rotating-an-oauth-client-id-and-secret).
- A correct OAuth client does not add permissions to an existing Dapier
  connection. After the key was corrected, account verification still needed
  the connection's `account_info.read`; the invoice flow also needed
  `files.content.write`. The app's permission list already allowed all four,
  but the old `dropbox` connection requested only `files.content.read` and
  `files.metadata.read`. This mismatch caused **Account could not be
  verified**. Update the Dapier connection scopes as shown below, then
  reconnect and approve them.
- If Dropbox asks for a passkey and offers **Set up later**, use that option
  to continue the existing signed-in session. If **My apps** is empty, confirm
  the signed-in Dropbox account and create the dedicated app described above.
- Toggling app permissions does not save them until **Submit**. Adding a
  redirect URI does not save it until **Add**.
- After changing OAuth client credentials, `uv run dapier oauth-clients list`
  confirms Dapier stored the client but cannot validate the secret. A
  successful OAuth retry is the end-to-end check.

## Changing Dropbox scopes

First change the app's **Permissions** and click **Submit**. Then replace the
connection's full scope list and reconnect it:

```powershell
uv run dapier connections scopes dropbox --scopes account_info.read `
  files.metadata.read files.content.read files.content.write
uv run dapier connections connect dropbox --agent <agent-name>
```

The console path is **Connectors → Edit** followed by **Reconnect**. For
multiple Dropbox connections, use each connection ID in the CLI command.
Removing access requires updating the list, revoking the existing token, and
reconnecting. Review the workflow before removing write access: it deletes
processed files. The [shared scope guide](README.md#changing-requested-scopes)
explains the source files that define defaults and validation.
