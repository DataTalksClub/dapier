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

---

## 1. One-time setup: the shared OAuth clients

Google-based connectors (Calendar and YouTube share one client) and Dropbox
each need an OAuth client created once in the provider's own console. Client
IDs and secrets are stored in the credentials table and take effect
immediately — no redeploy.

### Google (Calendar + YouTube)

1. Open the [Google Cloud Console](https://console.cloud.google.com/apis/credentials)
   and pick (or create) the project Dapier should authenticate against.
2. **APIs & Services → Library**: enable **Google Calendar API** and
   **YouTube Data API v3** (enable only what you use).
3. **APIs & Services → OAuth consent screen**: configure the app, add your
   DTC accounts as test users if the app is still in *Testing* mode.
4. **APIs & Services → Credentials → Create credentials → OAuth client ID**,
   type **Web application**. Under *Authorized redirect URIs* add exactly:

   ```
   https://dapier.dtcdev.click/oauth/callback
   ```

   This URI is fixed — it comes from the stack's `OAUTH_CALLBACK_URL` and both
   the console and the CLI drive consent through it.
5. Copy the **client ID** and **client secret**.
6. In the Dapier console open **Credentials → OAuth clients**, click
   **Set up** on the *Google (also YouTube)* row, paste both values, save.

### Dropbox

1. Open the [Dropbox App Console](https://www.dropbox.com/developers/apps) and
   **Create app** (scoped access, app folder or full Dropbox as the workflows
   need).
2. On the **Permissions** tab grant `files.metadata.read` and
   `files.content.read` (plus `files.content.write` if workflows upload).
3. On the **Settings** tab add the same redirect URI:
   `https://dapier.dtcdev.click/oauth/callback`.
4. Copy the **App key** and **App secret**.
5. In the Dapier console, **Credentials → OAuth clients → Set up** on the
   *Dropbox* row, paste both values, save.

### Notes

- **YouTube shares the Google client** — one Google entry covers both.
- The deploy-time environment (`GOOGLE_OAUTH_CLIENT_ID`/`..._SECRET`,
  `DROPBOX_OAUTH_CLIENT_ID`/`..._SECRET` exported before `make deploy`)
  remains a fallback so a fresh stack works before anyone configures anything.
  Values set in the console win.
- Rotating the Dropbox client in the console also re-webs Dropbox: during a
  rotation window the ingress accepts both the configured and the deploy-time
  secret.

Slack needs no OAuth client at all — see section 3.

---

## 2. Connecting an OAuth connector (Google Calendar, YouTube, Dropbox)

The console's **Connectors** view shows one **Create new** card per connector.
Creating never clobbers an existing connection: Dapier derives the first free
ID (`google-calendar`, then `google-calendar-2`, …).

### Google Calendar, step by step

1. Open the Dapier console → **Connectors**.
2. On the **Google Calendar** card click **Create new**.
3. Dapier provisions the connection with its standard scopes
   (`calendar.freebusy`, `calendar.events.owned`, `userinfo.email`) and
   redirects you straight to Google's consent screen.
4. Pick the Google account the connection should be bound to and approve.
5. You land back in the console; the connection now shows **connected** and
   the verified account email.

### YouTube, step by step

1. **Connectors → YouTube → Create new.**
2. Approve the consent screen with `youtube.readonly`.
3. Verification reads your channel via the YouTube Data API. If you are
   returned no channel, the account is not the channel owner — sign in with
   the **Brand Account** that owns the channel and reconnect.
4. The connection turns **connected** and records the channel ID and title.

### Dropbox, step by step

1. **Connectors → Dropbox → Create new.**
2. Approve the consent screen (`files.metadata.read`, `files.content.read`).
   Dapier always requests offline access, so the refresh token is long-lived.
3. The connection turns **connected** and records the Dropbox account ID.

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
