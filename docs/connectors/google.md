# Google connectors

Google Calendar, YouTube, Drive, Docs, and Sheets use a shared Google OAuth
client, but each Dapier connection has its own account binding, requested
scopes, and token. The Google Cloud project configuration allows the client to
request scopes; it does not grant them to an account until that account
completes consent.

See the [shared connector procedures](README.md) for OAuth credential
rotation, CLI operations, and the general process for changing scopes.

## Current configuration

- Google Cloud project: **`dtcdev-click`**.
- Existing Google clients are shared at the project level. The dedicated
  Dapier client is a Web application named `Dapier`; leave the separate
  `DTC DEV Auth` client and its `auth.dtcdev.click` callback unchanged.
- OAuth callback: `https://dapier.dtcdev.click/oauth/callback`.
- Enabled APIs: Google Calendar API, YouTube Data API v3, Google Drive API,
  Google Docs API, and Google Sheets API.
- OAuth audience: External, **Testing** at last verification.
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
   immediately with the [credential rotation procedure](README.md#updating-or-rotating-an-oauth-client-id-and-secret).
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
a Sheets row-append action, but no general Docs editor or Drive file watcher.
Configuring OAuth scopes does not implement those workflow features. See
Google's [Docs scopes](https://developers.google.com/workspace/docs/api/auth),
[Sheets scopes](https://developers.google.com/workspace/sheets/api/scopes),
and [Drive change notifications](https://developers.google.com/workspace/drive/api/guides/manage-changes).

## The two Workspace account connections

Connections are separate by account. Changing a scope list does not update an
already issued refresh token; reconnect each account and accept the new
consent screen.

### Gmail: `google-calendar`

Display name: **Google Calendar + Drive (Gmail)**. This connection keeps the
Calendar scopes and requests Drive, Docs, and Sheets scopes as well:

```powershell
uv run dapier connections scopes google-calendar --scopes `
  https://www.googleapis.com/auth/calendar.freebusy `
  https://www.googleapis.com/auth/calendar.events.owned `
  https://www.googleapis.com/auth/drive.readonly `
  https://www.googleapis.com/auth/documents `
  https://www.googleapis.com/auth/spreadsheets `
  https://www.googleapis.com/auth/userinfo.email
```

The stored Gmail token had the previous Calendar-only grants at last check.
Reconnect as `alexey.s.grigoriev@gmail.com` and accept the expanded consent
screen to grant Drive, Docs, and Sheets. Confirm the Google account before
approving, then inspect `uv run dapier connections show google-calendar` to
verify the token's granted scopes.

### DataTalks.club: `google-sheets`

Display name: **Google Drive, Docs & Sheets (DataTalks)**. This connection
requests only Workspace scopes and account identity:

```text
https://www.googleapis.com/auth/drive.readonly
https://www.googleapis.com/auth/documents
https://www.googleapis.com/auth/spreadsheets
https://www.googleapis.com/auth/userinfo.email
```

At last check it was **setup incomplete**, with no verified account. Connect
it while signed in as `alexey@datatalks.club`:

```powershell
uv run dapier connections connect google-sheets --agent <agent-name>
```

Check the Google account on the consent screen before approving. Then inspect
`uv run dapier connections show google-sheets` to verify which scopes the token
actually received.

### Calendar, YouTube, and other accounts

- Create **Google Calendar → Create new** in the console. It requests
  `calendar.freebusy`, `calendar.events.owned`, and `userinfo.email` by
  default. Approve from the Google account that should own this connection.
- Create **YouTube → Create new** and approve `youtube.readonly`. Dapier
  verifies the channel. If it finds no channel, use the Brand Account that
  owns it and reconnect.
- If the Google account has no usable refresh token yet, complete the consent
  flow once even if the client is already configured.

## Google OAuth publishing status

At last verification, `dtcdev-click` was an External app in **Testing**. Google
expires refresh tokens for Testing apps after seven days when consent requests
scopes beyond basic identity; affected accounts must consent again weekly
until the app leaves Testing. See Google's [audience and publishing status
guide](https://support.google.com/cloud/answer/15549945?hl=en).

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

No logo is required to publish. Uploading one starts an app verification
requirement, so do not add one unless it is part of a planned verification.

### Moving the project out of Testing

Publishing is a project-wide change: it also affects `DTC DEV Auth` and any
other OAuth client in `dtcdev-click`. Google recommends separating test and
production projects. The `drive.readonly` scope is Restricted and may require
verification and a security assessment depending on app use. Unverified
sensitive or restricted scopes can show users a warning and enforce a
100-new-user cap. Test-user status does not remove those production limits.

When the project owner has approved this project-wide change, open **Google
Auth Platform → Audience → Publish app**, review the confirmation, publish,
and verify Audience shows **In production**. Last checked, the project was
still **Testing**. The request to publish was left pending final user
confirmation, so no **Publish app** action was completed. Do not assume the
change occurred just because the branding URLs are ready. See Google's
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

For where to change default scopes in code and the common CLI workflow, use the
[shared connector guide](README.md#where-scope-behavior-lives-in-the-repository).
