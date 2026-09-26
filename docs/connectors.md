# Connectors

Connectors are the products workflows hook into. Each configured instance is a
**connection**: a named record (a `connection_id`, a provider, a display name,
scopes, and the verified provider account) that points at a token in the
write-only credentials table. The connections table itself never holds secrets.

Agents never see provider apps or passwords — a workflow action says
`connection_id: youtube`, Dapier resolves the stored token, refreshes it if it
is expired, and checks that the agent holds a grant for that connection before
handing anything out.

Today's connectors:

| Connector  | Auth model      | Used for                                        | `connection_id` example |
|------------|-----------------|-------------------------------------------------|-------------------------|
| Google Calendar | OAuth (Google client) | Free/busy lookups, owned-event edits | `google-calendar`       |
| YouTube    | OAuth (Google client) | Published-video triggers for a channel | `youtube`               |
| Dropbox    | OAuth (Dropbox client) | File watchers, uploads, invoice pipeline | `dropbox`               |
| Slack      | Pasted bot/user token | Posting notifications                     | `slack`                 |
| Zoom       | Webhook Secret Token | Cloud recording video ready trigger       | `zoom`                  |

---

## 1. One-time setup: the shared OAuth clients

Google-based connectors (Calendar and YouTube share one client) and Dropbox
each need an OAuth client created once in the provider's own console. Client
IDs and secrets are stored in the credentials table and take effect
immediately — no redeploy.

### Google (Calendar + YouTube)

1. Open the [Google Cloud Console](https://console.cloud.google.com/apis/credentials)
   and select the `dtcdev-click` project.
2. In **APIs & Services → Library**, enable the APIs used by Dapier before
   editing consent scopes. The `dtcdev-click` project currently has **Google
   Calendar API**, **YouTube Data API v3**, **Google Drive API**, **Google Docs
   API**, and **Google Sheets API** enabled. The scope picker only offers
   scopes for APIs already enabled in the project.
3. Open **Google Auth Platform → Audience**. The `dtcdev-click` app is External
   and currently in *Testing* mode. Under **Test users**, the configured
   accounts are `alexey.s.grigoriev@gmail.com` and
   `alexey@datatalks.club`. To add another account, click **Add users**, enter
   the address, press **Enter** so it becomes a chip, then click **Save**.
   The project-wide consent branding currently says `DTC`; it is shared with
   existing OAuth clients, so don't change it just for Dapier.
4. Open **Google Auth Platform → Data access** and click **Add or remove
   scopes**. Ensure the exact Dapier scopes listed below are present in the
   project-wide scope table:
   `https://www.googleapis.com/auth/calendar.freebusy`,
   `https://www.googleapis.com/auth/calendar.events.owned`,
   `https://www.googleapis.com/auth/userinfo.email`,
   `https://www.googleapis.com/auth/youtube.readonly`,
   `https://www.googleapis.com/auth/drive.readonly`,
   `https://www.googleapis.com/auth/documents`, and
   `https://www.googleapis.com/auth/spreadsheets`. Keep this project-wide list
   in sync with each Dapier connection's requested scopes. Do not remove scopes
   that another client in this project still uses. See Google's [consent and scope
   setup guide](https://developers.google.com/workspace/guides/configure-oauth-consent)
   and [OAuth policies](https://developers.google.com/identity/protocols/oauth2/policies).
   In the scope dialog, choose **Add to table**, then **Update**; back on the
   Data access page, click **Save** to persist the change.
5. In **Google Auth Platform → Clients**, select the existing `Dapier` client
   if present; otherwise click **Create client**, choose **Web application**,
   and create a client named `Dapier`. Don't modify the existing `DTC DEV Auth`
   client, whose redirect URI is for `auth.dtcdev.click`. Under *Authorized
   redirect URIs* add exactly:

   ```
   https://dapier.dtcdev.click/oauth/callback
   ```

   This URI is fixed — it comes from the stack's `OAUTH_CALLBACK_URL` and both
   the console and the CLI drive consent through it.
6. Copy the new **client ID** and **client secret**. Google's console won't
   show the secret again later. Store both immediately with the credential
   update procedure below.

### Google Drive, Docs, and Sheets scope meanings

The `dtcdev-click` consent configuration includes these Workspace scopes:

| Scope | Access granted | Google classification |
|-------|----------------|-----------------------|
| `https://www.googleapis.com/auth/drive.readonly` | Read and download all files in the consenting account's Drive; also accepted by the Drive Changes API, including `changes.watch` | Restricted |
| `https://www.googleapis.com/auth/documents` | Read, create, edit, and delete all Google Docs in the consenting account | Sensitive |
| `https://www.googleapis.com/auth/spreadsheets` | Read, create, edit, and delete all Google Sheets in the consenting account | Sensitive |

There is no separate Drive scope for watching changes: `drive.readonly` is
accepted by the Drive Changes API. A watch notification signals that changes
are available; a workflow still needs code to fetch and process the changes
feed. Dapier currently has a Sheets row-append action, but no general Docs
editor or Drive file watcher. Adding consent scopes alone does not add those
workflow features. See Google's [Docs scopes](https://developers.google.com/workspace/docs/api/auth),
[Sheets scopes](https://developers.google.com/workspace/sheets/api/scopes),
and [Drive change notifications](https://developers.google.com/workspace/drive/api/guides/manage-changes).

### Google OAuth publishing status

`dtcdev-click` remains an External app in **Testing**. Google expires refresh
tokens for Testing apps after seven days when consent includes scopes beyond
basic identity. This is why each consenting account needs to authorize again
weekly until the app leaves Testing. See Google's [audience and publishing
status guide](https://support.google.com/cloud/answer/15549945?hl=en).

The Google Auth Platform **Branding** page needs these public links before
publishing:

| Branding field | URL |
|----------------|-----|
| Application home page | `https://dapier.dtcdev.click/about` |
| Application privacy policy | `https://dapier.dtcdev.click/privacy` |
| Application Terms of Service | `https://dapier.dtcdev.click/terms` |

The pages are implemented as public static routes before authentication in
`src/dapier/api/router.py`; their API Gateway GET routes are in `template.yaml`.
The root `/` remains the authenticated console. The pages are deployed and each
URL was fetched without browser credentials: all returned HTTP 200 with no
redirect. These exact URLs are now saved in **Google Auth Platform → Branding**.
The domain `dtcdev.click` is already authorized. The displayed app name/support
email and the `DTC DEV Auth` client are shared by this Google project. Switching
its audience to Production affects every OAuth client in `dtcdev-click`; Google
recommends separate testing and production projects. `drive.readonly` is a
Restricted scope, so production may require verification and, depending on the
app's use, a security assessment. See Google's [production readiness
overview](https://developers.google.com/identity/protocols/oauth2/production-readiness/overview)
and [restricted-scope verification requirements](https://developers.google.com/identity/protocols/oauth2/restricted-scope-verification).

After the public links are saved, the status change is **Google Auth Platform →
Audience → Publish app**. Confirm the dialog and verify that Audience shows
**In production**. Production removes the Testing seven-day refresh-token
expiry. If sensitive or restricted scopes remain unverified, Google may show
an unverified-app warning and enforce a 100-new-user cap until verification;
existing test-user status does not remove those restrictions. Treat this as a
project-wide change because it applies to `DTC DEV Auth` as well as Dapier.

The Google Branding page does not require an app logo to publish. Uploading a
logo starts a verification requirement, so do not add one just to publish.
If a separate production app needs a logo later, make it Dapier-specific.

### Dropbox

1. Open the [Dropbox App Console](https://www.dropbox.com/developers/apps) and
   open **My apps**. Select the existing **Dapier DTC Dev** app if present;
   otherwise create it with these settings: choose **Scoped access** for the
   API type and **Full Dropbox** for the access type. The enabled
   `dropbox_on_upload`
   workflow watches `/_dtc_paperwork/income-invoices/` and deletes each
   processed file, so the current workflow needs **Full Dropbox** access; an
   app-folder app cannot reach that root-level path. Dropbox treats access type
   (App Folder vs Full Dropbox) separately from scopes, and changing access
   type requires deleting and recreating an app; create a separate Full Dropbox
   app if the existing app is App Folder. The new-app form requires checking
   **I agree to the Dropbox API Terms and Conditions** before it will create
   the app. See Dropbox's [App Console
   guide](https://docs.dropboxapi.com/dropbox-api/docs/get-started/tutorial/app-console)
   and [content access and scope guide](https://docs.dropboxapi.com/dropbox-api/docs/oauth).
   Use app-folder access only after moving the workflow's Dropbox paths under
   the app folder.
2. On **Permissions**, grant `files.metadata.read`, `files.content.read`,
   and `files.content.write`; `account_info.read` is enabled by default and
   may appear checked but disabled. Click **Submit** at the bottom to save
   scope changes. Dapier uses `account_info.read` to verify the connected
   account; the enabled workflow needs `files.content.write` to delete
   processed files.
3. On **Settings**, under **OAuth 2 → Redirect URIs**, add exactly
   `https://dapier.dtcdev.click/oauth/callback` and click **Add** to commit the
   URI.
4. Copy the **App key**. Click **Show** beside **App secret**, then copy the
   secret. Store both immediately with the credential update procedure below.
   The current `Dapier DTC Dev` app key is `aia0r7fjj4ls6j9`; check that the
   console still shows this key before updating Dapier.

### Updating or rotating OAuth client credentials

Use this procedure when a client secret was entered incorrectly, a provider
secret was rotated, or a client ID changed. The ID and secret must come from
the same provider app: the Dropbox **App key** is the OAuth client ID, and its
matching **App secret** is the client secret. `oauth-clients set` replaces
both stored values together and takes effect immediately; it does not require
a redeploy. The command reads the secret from stdin or a file, never from a
command-line argument.

For Dropbox, open **App Console → Dapier DTC Dev → Settings → OAuth 2**, copy
the current **App key**, click **Show** beside **App secret**, and copy the
current secret. For Google, use **Google Auth Platform → Clients → Dapier**;
create/reset a client secret there if the old value is unavailable (Google
does not reveal an existing secret again). Then in Windows PowerShell, copy
only that fresh secret to the Windows clipboard and run the matching provider
command. Example for Dropbox:

```powershell
$secret = Get-Clipboard -Raw
if ([string]::IsNullOrWhiteSpace($secret)) { throw 'Windows clipboard is empty; copy the current app secret first.' }
try {
  $secret | uv run dapier oauth-clients set dropbox --client-id 'aia0r7fjj4ls6j9' --client-secret-file -
  if ($LASTEXITCODE -ne 0) { throw 'Dapier did not store the Dropbox OAuth client.' }
} finally {
  Remove-Variable secret -ErrorAction SilentlyContinue
  Set-Clipboard -Value ''
}
```

For Google, change `dropbox` to `google` and replace the client ID with the
Google `Dapier` client ID. `--client-secret-file -` means stdin; Dapier strips
surrounding whitespace. If `uv` is not on this PowerShell session's `PATH`,
use the installed executable explicitly, for example
`& "$env:USERPROFILE\.local\bin\uv.exe" run dapier ...`.

Copy the secret from the provider console immediately before running the
command. Chrome automation may copy into a browser-only clipboard that
PowerShell cannot read; if `Get-Clipboard -Raw` is empty, stop and copy through
the Windows clipboard or use a private temporary file with
`--client-secret-file <path>`, then delete that file. Do not paste the secret
into command arguments, shell history, chat, or repository files. Afterward,
run `uv run dapier oauth-clients list` to confirm the provider says
**configured**. This confirms storage only, not that the provider accepts the
secret; finish by retrying the OAuth setup. The set command's success message
also confirms the update was stored, and the saved secret cannot be read back.

### Setup notes for the next run

- Use the existing signed-in Chrome session and authenticated `uv run dapier`
  CLI. Start with `uv run dapier oauth-clients list`; reuse the existing
  provider clients when configured instead of creating duplicates.
- Use the current **Google Auth Platform** navigation. The older **APIs &
  Services → OAuth consent screen** entry may redirect to Auth Platform
  Overview; use **Audience**, **Data access**, and **Clients** in its menu.
- The Google console can show a blank content pane briefly while a section
  loads. Wait a few seconds before retrying navigation; a short blank state did
  not mean the project or page was unavailable.
- Before switching Google Auth Platform from Testing to Production, complete
  the Branding links in the publishing-status section. The status belongs to
  the whole `dtcdev-click` project and affects the separate `DTC DEV Auth`
  client too. Production removes the seven-day Testing refresh-token expiry,
  but `drive.readonly` is Restricted and may need verification. Google currently
  recommends separating test and production projects.
- The public Google OAuth URLs are `/about`, `/privacy`, and `/terms`. They are
  static routes served before sign-in in `src/dapier/api/router.py`; the root
  `/` is the operator console and is not the OAuth homepage. Check the three
  pages with an unauthenticated GET before saving them in Google Branding.
- Enable each required API before adding scopes. Adding a scope has
  three commits: **Add to table → Update → Save**. Adding a test user also
  needs **Enter** to turn the address into a chip before **Save**.
- If Dropbox asks for a passkey during sign-in and offers **Set up later**, use
  that option to continue the existing session. An initially empty **My apps**
  page means there is no app in that account yet; create the dedicated app
  described above.
- Dropbox scope changes are not saved by toggling permissions alone; click
  **Submit**. A redirect URI must also be committed from **Settings** after
  adding it.
- Before starting, run `uv run dapier oauth-clients list`; afterward run it
  again and confirm Google and Dropbox both say **configured**. This verifies
  storage without exposing either secret. It does not validate that a copied
  secret matches the provider app; a successful OAuth retry is the end-to-end
  check.
- If Dropbox reports **Account could not be verified** after correcting the
  client key/secret, check the existing connection's requested scopes too.
  The connected account lookup needs `account_info.read`; the invoice workflow
  also needs `files.metadata.read`, `files.content.read`, and
  `files.content.write`. A correct OAuth client does not add scopes to an
  existing connection. Set the exact list in **Connectors → Edit** or with
  `uv run dapier connections scopes dropbox --scopes account_info.read
  files.metadata.read files.content.read files.content.write`, then reconnect
  and approve the updated request. The provider app's **Permissions** must
  allow the same scopes, and Dropbox saves those changes only after **Submit**.
  In the previous setup, the app permissions were already correct, but the
  existing `dropbox` connection requested only `files.content.read` and
  `files.metadata.read`; that mismatch was why retrying with the corrected
  secret still failed account verification.
- Setting shared OAuth clients does not connect a Google or Dropbox account.
  Each account still needs **Connectors → Create new** and provider consent.
- Keep the existing `DTC` consent branding and `DTC DEV Auth` client
  unchanged. Use the dedicated Dapier client and callback URI above.

### Approval for repeating the DTC Dev setup

The user explicitly authorized repeating the following configuration without
asking again, including accepting the Dropbox API Terms checkbox at app
creation:

- Google Cloud project `dtcdev-click`, External / Testing app, test users
  `alexey.s.grigoriev@gmail.com` and `alexey@datatalks.club`, with Calendar,
  YouTube, Drive, Docs, and Sheets APIs enabled. The Data access table includes
  scopes
  `https://www.googleapis.com/auth/calendar.freebusy`,
  `https://www.googleapis.com/auth/calendar.events.owned`,
  `https://www.googleapis.com/auth/userinfo.email`,
  `https://www.googleapis.com/auth/youtube.readonly`,
  `https://www.googleapis.com/auth/drive.readonly`,
  `https://www.googleapis.com/auth/documents`, and
  `https://www.googleapis.com/auth/spreadsheets`; dedicated Web client `Dapier`
  with callback `https://dapier.dtcdev.click/oauth/callback`.
- The same Dropbox account, `Dapier DTC Dev`, Scoped access / Full Dropbox,
  scopes `account_info.read`, `files.metadata.read`, `files.content.read`,
  and `files.content.write`, with callback
  `https://dapier.dtcdev.click/oauth/callback`.

This approval is limited to these accounts, app names, access type, scopes, and
callback. Ask for fresh direction if Dropbox terms materially change, or if
the account, app, access type, or scopes differ. Store secrets only through the
authenticated CLI command above; verify configuration with
`uv run dapier oauth-clients list`.

### Notes

- **YouTube shares the Google client** — one Google entry covers both.
- `uv run dapier oauth-clients list` reports whether each client is configured
  without returning client secrets.
- The deploy-time environment (`GOOGLE_OAUTH_CLIENT_ID`/`..._SECRET`,
  `DROPBOX_OAUTH_CLIENT_ID`/`..._SECRET` exported before `make deploy`)
  remains a fallback so a fresh stack works before anyone configures anything.
  Values set in the console win.
- Rotating the Dropbox client in the console also re-webs Dropbox: during a
  rotation window the ingress accepts both the configured and the deploy-time
  secret.

Slack needs no OAuth client at all — see section 3.
Zoom also needs no OAuth client: it uses a webhook signing token from a Zoom app.

### Zoom cloud recording trigger

1. Enable cloud recording for the Zoom host. In the Zoom App Marketplace, create
   a **Webhook Only** app with an event subscription for `recording.completed`.
2. In Dapier **Connections → Zoom → Add account**, paste the app's **Secret
   Token**. Dapier stores it write-only and shows the connection's endpoint URL
   (`https://dapier.dtcdev.click/hooks/zoom/<connection-id>`).
3. Paste that URL as the Zoom Event Notification Endpoint URL and click
   **Validate** in Zoom. Dapier answers Zoom's signed challenge. Save the event
   subscription. The connection then shows **connected**.
4. Create a workflow with `connector: zoom` and `event: recording.completed`,
   or choose **Zoom → recording.completed** in the designer. Dapier emits one
   event per completed cloud recording that contains at least one MP4 or M4V
   video. The event contains `connection_id`, `account_id`, `meeting_id`,
   `meeting_uuid`, `topic`, `host_email`, `share_url`, and `video_files` metadata.
   Download tokens are never placed in workflow events.

The same setup is available from the CLI:

```sh
uv run dapier connections import zoom --provider zoom --token-file /path/to/zoom-secret
```

The CLI prints the endpoint URL. Use `--display-name` to name additional Zoom
apps. Replacing the secret returns the connection to **setup incomplete** until
Zoom validates it again. Dapier validates each webhook's signature and rejects
requests more than five minutes from their signed timestamp. See
[Zoom's webhook setup and validation guide](https://developers.zoom.us/docs/api/webhooks/).

---

## 2. Connecting an OAuth connector (Google Calendar, YouTube, Dropbox)

The console's **Connectors** view shows one **Create new** card per connector.
Creating never clobbers an existing connection: Dapier derives the first free
ID (`google-calendar`, then `google-calendar-2`, …).

The CLI can provision OAuth connection metadata and then open the same provider
consent flow:

```sh
uv run dapier connections create youtube-team --provider youtube \
  --scopes https://www.googleapis.com/auth/youtube.readonly
uv run dapier connections connect youtube-team --agent <agent-name>
```

For Slack or Telegram, create or replace a connection with
`uv run dapier connections import <connection-id> --provider <provider>
--token-file <private-file>`; add `--display-name` to set its label.

### Google Calendar, step by step

1. Open the Dapier console → **Connectors**.
2. On the **Google Calendar** card click **Create new**.
3. Dapier provisions the connection with its standard scopes
   (`calendar.freebusy`, `calendar.events.owned`, `userinfo.email`) and
   redirects you straight to Google's consent screen.
4. Pick the Google account the connection should be bound to and approve.
5. You land back in the console; the connection now shows **connected** and
   the verified account email.

### Google Drive, Docs, and Sheets connections

The Gmail Google connection (`google-calendar`, display name **Google Calendar
+ Drive (Gmail)**) keeps its Calendar scopes and also requests the three
Workspace scopes. Its configured request is:

```powershell
uv run dapier connections scopes google-calendar --scopes `
  https://www.googleapis.com/auth/calendar.freebusy `
  https://www.googleapis.com/auth/calendar.events.owned `
  https://www.googleapis.com/auth/drive.readonly `
  https://www.googleapis.com/auth/documents `
  https://www.googleapis.com/auth/spreadsheets `
  https://www.googleapis.com/auth/userinfo.email
```

Updating this list does not update the token already held for Gmail. Reconnect
`google-calendar` and approve the new consent screen while signed in as
`alexey.s.grigoriev@gmail.com` to grant the scopes.

The second account has a separate Google connection (`google-sheets`, display
name **Google Drive, Docs & Sheets (DataTalks)**). Its saved request is
`drive.readonly`, `documents`, `spreadsheets`, and `userinfo.email`; it has no
Calendar or YouTube scopes. The record is currently **setup incomplete** with
no verified account. Connect it with:

```powershell
uv run dapier connections connect google-sheets --agent <agent-name>
```

Complete consent while signed in as `alexey@datatalks.club`. Check the account
shown by Google before approving so the token is bound to the intended account.
The connection's configured scope list and the scopes actually granted to its
token are separate; inspect `dapier connections show google-sheets` after
consent and reconnect if the account granted fewer scopes.

### YouTube, step by step

1. **Connectors → YouTube → Create new.**
2. Approve the consent screen with `youtube.readonly`.
3. Verification reads your channel via the YouTube Data API. If you are
   returned no channel, the account is not the channel owner — sign in with
   the **Brand Account** that owns the channel and reconnect.
4. The connection turns **connected** and records the channel ID and title.

### Dropbox, step by step

1. **Connectors → Dropbox → Create new.**
2. Approve the consent screen (`account_info.read`, `files.metadata.read`,
   `files.content.read`, `files.content.write`). Dapier always requests offline
   access, so the refresh token is long-lived.
3. The connection turns **connected** and records the Dropbox account ID.

### Changing requested scopes

Scopes are configured in two places. The provider project/app must allow each
scope, and each Dapier connection requests only the scopes it needs. The OAuth
client ID and secret do not contain the scope list.

To add a scope:

1. Enable the provider scope first. In Google Cloud, use **Google Auth
   Platform → Data access**. In Dropbox, use the app's **Permissions** tab and
   save/submit the changes. Dropbox app permissions set the scopes the app may
   request; Dapier sends the connection's selected scope list during consent.
2. In Dapier open **Connectors → Edit** and replace the connection's scope list,
   or use the CLI. The focused command is:

   ```sh
   uv run dapier connections scopes <connection-id> --scopes <scope> [<scope> ...]
   ```

   The general `uv run dapier connections edit <connection-id>` command can
   also change `--display-name`, `--scopes`, or a Dropbox `--root-path` (use
   `--clear-root-path` to list from the Dropbox root).

   For example, to add Dropbox write access to the current standard scopes:

   ```sh
   uv run dapier connections scopes dropbox --scopes account_info.read \
     files.metadata.read files.content.read files.content.write
   ```

3. Reconnect the connection and approve the updated consent request. From the
   console use **Reconnect**; from the CLI run
   `uv run dapier connections connect <connection-id> --agent <agent-name>`.
   The Dapier connection stores the requested scope list separately from the
   OAuth client credentials. Changing a preset affects newly created
   connections only; edit existing connections explicitly.

For Dropbox's current invoice connection, the complete requested list is
`account_info.read`, `files.metadata.read`, `files.content.read`, and
`files.content.write`. This list is needed even when the Dropbox app already
allows all four scopes: provider permissions and the Dapier connection's
requested scopes are separate settings. In particular, without
`account_info.read`, Dropbox account verification can fail with **Account
could not be verified**; without `files.content.write`, the workflow cannot
delete processed invoices.

To remove a scope, update the Dapier connection list, revoke its old token, and
reconnect it so the provider issues a token for the reduced access:

```sh
uv run dapier connections scopes <connection-id> --scopes <remaining-scope> [<scope> ...]
uv run dapier connections revoke <connection-id>
uv run dapier connections connect <connection-id> --agent <agent-name>
```

The console's **Revoke** action does the same as `connections revoke`. Keep
required identity scopes: Google Calendar needs
`https://www.googleapis.com/auth/userinfo.email`, YouTube needs a channel scope
such as `https://www.googleapis.com/auth/youtube.readonly`, and Dropbox needs
`account_info.read` for account verification. Dropbox file watching needs
`files.metadata.read`; reading files needs `files.content.read`; uploading or
deleting files needs `files.content.write`.

### Where to make future scope changes

Use this map when a provider or workflow needs another permission:

| Change | Update |
|--------|--------|
| Default scopes on newly created connections | `CONNECT_PROVIDERS` in [`src/web/js/views/connections.js`](../src/web/js/views/connections.js) |
| Scopes on an existing connection | Console **Connectors → Edit**, or `uv run dapier connections scopes <connection-id> --scopes <scope> [<scope> ...]` (`connections edit` can also update scopes) |
| Scopes allowed by Google consent | Google Cloud project `dtcdev-click` → **Google Auth Platform → Data access** |
| Scopes allowed by Dropbox | Dropbox App Console → app **Permissions**; check the app's Full Dropbox vs App Folder access type too |
| Scope validation, identity scopes, and provider consent behavior | [`src/dapier/connections/records.py`](../src/dapier/connections/records.py) and [`src/dapier/connections/providers/oauth_providers.py`](../src/dapier/connections/providers/oauth_providers.py) |
| File access the invoice workflow needs | [`workflows/dropbox_on_upload.yaml`](../workflows/dropbox_on_upload.yaml) and its Dropbox actions |

After changing an existing connection's scopes, reconnect it. To reduce
permissions, first update the requested list, run `uv run dapier connections
revoke <connection-id>`, then reconnect; revocation attempts provider-side
token revocation and clears Dapier's stored tokens. Check all workflows using
that connection before removing a scope. The Dropbox [get current account
endpoint](https://docs.dropboxapi.com/dropbox-api/api-reference/user-endpoints/users/get-current-account)
requires `account_info.read` for Dapier's identity verification.

For Dropbox, adding an app permission does not add it to tokens already issued;
those connections must reconnect. For Google, update **Data access** to match
the scopes the app requests. Published External apps may need Google's review
for sensitive or restricted scopes. The current `dtcdev-click` app remains in
Testing, so every account that completes consent must be in its test-user list.

### Expected-account binding (optional)

Every OAuth connection is bound to the account that first consented. To pin a
connection to a known account up front, set `expected_account_id` (via the
API/CLI import path below). A consent from any other account is rejected with
409 before the connection is marked connected. The binding is immutable
afterwards: pointing the connection at a different account requires an
explicit binding replacement on reconnect.

---

## 3. Connecting Slack (pasted token)

Slack skips the browser consent round trip — you paste a token, Dapier
verifies it against Slack, and the connection is immediately usable.

1. In Slack: create an app at [api.slack.com/apps](https://api.slack.com/apps)
   (or pick an existing one), and under **OAuth & Permissions** grant the bot
   at least **`chat:write`** (add `channels:read`/`groups:read` as needed).
2. Install the app to the workspace and copy the **Bot User OAuth Token**
   (`xoxb-…`). A user token (`xoxp-…`) also works.
3. Invite the bot to the channels it should post in (`/invite @your-bot`).
4. In the Dapier console: **Connectors → Slack → Create new.**
5. Paste the token into the dialog and save.
6. Dapier verifies it with Slack's `auth.test`; on success the token is
   stored under the connection's credential ID and the connection shows
   **connected** with the workspace identity. A bad or revoked token is
   rejected before anything is stored.

---

## 4. The CLI does the same things

Every console action is reachable through the `dapier` CLI, driving the same
operator-gated `/api/agent/*` API:

```bash
# Consent flow for an OAuth connection (opens the browser, waits for consent)
dapier connections connect google-calendar --agent buildcamp

# Operator import of an existing refresh token (e.g. issued by another client)
dapier connections import youtube-personal \
  --provider youtube \
  --authorized-user-file refresh-token.json \
  --expected-account UCxxxxxxxx \
  --scopes https://www.googleapis.com/auth/youtube.readonly

# The global Slack credential used by the default posting action
dapier credentials set slack --file slack-token.txt   # or --file - for stdin

# Inspect and revoke
dapier connections list
dapier connections show youtube
dapier connections revoke youtube-personal
```

`import` with `--client-id`/`--client-secret-file` is only needed for refresh
tokens issued by a different OAuth client than the shared one.

Grant an agent access to a connection, then use it:

```bash
dapier grants save grant.json
# {"connection_id": "youtube", "subject": "subject-9", "agent": "buildcamp", "operations": ["read", "write"]}

dapier token exec youtube --agent buildcamp -- yt-dlp --version
```

---

## 5. Statuses, edits, and lifecycle

A connection is one of:

- **awaiting consent** — created, but the provider consent has not been
  completed. Click **Connect** (or re-run `dapier connections connect`) to
  restart consent; the authorize URL expires after 10 minutes.
- **connected** — verified against the provider and usable.
- **expired — needs refresh** — the stored refresh token no longer works
  (revoked, password change, Google re-consent required). Click
  **Reconnect** on the card and approve again.

**Edit** on a connection lets you rename it, change the requested scopes
(hidden for Slack — Slack carries its scopes in the token), and replace the
Slack token. Tokens are stored write-only: nothing ever displays or returns
them.

Deleting a connection's access is `dapier connections revoke <id>` — it
revokes the token provider-side (where the provider supports it) and removes
the stored credential.
