# Connector setup and operations

Provider-specific setup and verified account details live in separate guides:

| Provider | Guide | Connection examples | Authentication |
|----------|-------|---------------------|----------------|
| Google | [Google Calendar, YouTube, Drive, Docs, Sheets, and Gmail](../../plugins/google/README.md) (plugin) | `google-calendar`, `google-sheets`, `youtube` | OAuth |
| Dropbox | [Dropbox](../../plugins/dropbox/README.md) (plugin) | `dropbox` | OAuth |
| Slack | [Slack](../../plugins/slack/README.md) (plugin) | `slack` | Pasted bot or user token |
| Zoom | [Zoom](../../plugins/zoom/README.md) (plugin) | `zoom-api` (OAuth), `zoom` (cloud-recording webhook) | OAuth for API access; separate webhook signing token for recordings |
| Telegram | [Telegram](../../plugins/telegram/README.md) (plugin) | `telegram` (e.g. `telegram-bot`) | Pasted BotFather bot token |
| Mailchimp | [Mailchimp](../../plugins/mailchimp/README.md) (plugin) | `mailchimp` (pseudo connection) | Stored API key; the `-usNN` suffix selects the datacenter |
| Amazon S3 | [AWS / S3](../../plugins/aws/README.md) (plugin) | `aws`, `s3` (pseudo connections) | Stored IAM access key pair |
| AI (LLM) | [AI](ai.md) | — (no connection; the env is the config) | `LLM_API_KEY` (+ optional `LLM_BASE_URL`/`LLM_MODEL`) |

This page covers the shared Dapier connection, scope, credential, and lifecycle
procedures. A connection is a named record (`connection_id`, provider, display
name, requested scopes, and verified provider account) that points to a token
in the write-only credentials table. The connections table never holds
secrets. Workflows refer to a connection ID; Dapier resolves and refreshes its
token and checks the agent's grant before using it.

## Shared OAuth client credentials

Google connectors share one Google OAuth client; Dropbox and Zoom each have a
separate OAuth client. Configure each client once in its provider console,
then save its ID and secret with `dapier oauth-clients set`. The values are
stored in the credentials table and take effect immediately; no redeploy is
needed.

The redirect URI for these OAuth clients is exactly:

```
https://dapier.dtcdev.click/oauth/callback
```

For client creation, scope requirements, and the verified state of each
provider app, see the [Google](../../plugins/google/README.md), [Dropbox](../../plugins/dropbox/README.md), and
[Zoom](../../plugins/zoom/README.md) guides. YouTube uses the shared Google client. Slack uses a
pasted token. Zoom's recording webhook uses a separate signing token in
addition to its OAuth client for Zoom API access. Telegram also uses a
pasted token, while Mailchimp and Amazon S3 use stored provider
credentials rather than connections — see their guides.

Before provider-console work, run `uv run dapier oauth-clients list` in the
already authenticated CLI session. Reuse configured clients rather than
creating duplicate apps. A **configured** result confirms storage only; it
does not connect an account or prove the provider will accept the secret.

### Updating or rotating an OAuth client ID and secret

Use this when a secret was entered incorrectly, a provider secret was rotated,
or the client ID changed. The ID and secret must come from the same provider
app. `oauth-clients set` replaces both values together. It reads the secret
from stdin or a file, never from a command-line argument, and does not require
a redeploy.

1. In the provider console, open the Dapier app and copy its current client ID.
   For Dropbox, the **App key** is the client ID. For Google, use the `Dapier`
   Web client in `dtcdev-click`. For Zoom, use the OAuth app's **Client ID**.
2. Copy the matching current secret. Dropbox lets you reveal its current app
   secret. Google does not reveal an existing client secret again; create a
   replacement secret if the value was lost. Use the provider-specific guide
   for the exact console path.
3. Immediately copy only the fresh secret to the Windows clipboard and run
   the matching command below in PowerShell. Replace the Dropbox example's
   key with the current app key if it changed. For Google or Zoom, use that
   OAuth client ID and change `dropbox` to `google` or `zoom`.

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

`--client-secret-file -` means read the secret from stdin; Dapier strips
surrounding whitespace. If `uv` is not on this PowerShell session's `PATH`,
use the installed executable explicitly, for example
`& "$env:USERPROFILE\.local\bin\uv.exe" run dapier ...`.

Chrome automation may copy to a browser-only clipboard that PowerShell cannot
read. If `Get-Clipboard -Raw` is empty, stop and copy through the Windows
clipboard, or use a private temporary file with
`--client-secret-file <path>` and delete that file afterward. Do not put
secrets in command arguments, shell history, chat, or repository files.
Afterward run:

```powershell
uv run dapier oauth-clients list
```

This confirms the client is stored as **configured** without returning its
secret. It does not prove that the provider accepts the secret. Retry the
connection's OAuth setup as an end-to-end check. Dropbox client rotation also
re-webhooks Dropbox: ingress accepts both the configured secret and the
deploy-time secret during the rotation window.

The deploy-time environment (`GOOGLE_OAUTH_CLIENT_ID` /
`GOOGLE_OAUTH_CLIENT_SECRET`, `DROPBOX_OAUTH_CLIENT_ID` /
`DROPBOX_OAUTH_CLIENT_SECRET`) remains a fallback for a fresh stack. Export
the variables before `make deploy` when the deployment should seed or rotate
them; omit them and CloudFormation keeps the previous values. Values set in
the console/CLI take precedence. The current deploy script does not seed the
Zoom OAuth client from environment variables; set it at runtime with
`uv run dapier oauth-clients set zoom` or **Credentials → OAuth clients**.

## Creating and connecting a provider account

The console's **Connectors** view has a **Create new** card for each OAuth
connector. Creating a connection does not overwrite an existing one: Dapier
chooses the first unused ID, such as `google-calendar`, then
`google-calendar-2`.

Provision metadata and launch the same provider consent flow with the CLI:

```sh
uv run dapier connections create youtube-team --provider youtube \
  --scopes https://www.googleapis.com/auth/youtube.readonly
uv run dapier connections connect youtube-team --agent <agent-name>
```

For Slack or Telegram, or to set up a Zoom recording webhook, import a token
with
`uv run dapier connections import <connection-id> --provider <provider>
--token-file <private-file>`. The connection is named by the verified
account (workspace, bot @username) — there is no separate display name.

Check provider guides for the approved accounts and scopes. Provider consent
is separate from configuring a shared OAuth client: each Google or Dropbox
account still needs its own connection and consent.

## Changing requested scopes

Scope permissions are configured in two places: the provider app must allow a
scope, and each Dapier connection requests only the scopes that it needs. The
OAuth client ID and secret do not contain the scope list.

To add or change a scope:

1. Allow the scope in the provider console. Google uses the Cloud project's
   **Google Auth Platform → Data access** scope table. Dropbox uses the app's
   **Permissions** tab; click **Submit** after changing it. Zoom uses the
   OAuth app's **Scopes** page. See the
   [Google scope instructions](../../plugins/google/README.md#google-cloud-scope-configuration) or
   [Dropbox permissions instructions](../../plugins/dropbox/README.md#app-configuration) or
   [Zoom OAuth instructions](../../plugins/zoom/README.md#zoom-oauth-for-api-access).
2. Replace the connection's scope list in **Connectors → Edit**, or run:

   ```sh
   uv run dapier connections edit <connection-id> --scopes <scope> [<scope> ...]
   ```

   Zoom API connections edit their scopes in Manage → Advanced like any
   OAuth connection; a Zoom webhook connection's Manage dialog holds its
   Secret Token instead. New Zoom API connections start from the default
   read scopes in `src/dapier/connections/zoom.py` (`API_DEFAULT_SCOPES`).

   `uv run dapier connections edit <connection-id>` can also change the
   display name, scopes, or Dropbox root path (`--clear-root-path` selects the
   Dropbox root).
3. Reconnect and approve the updated consent request. In the console use
   **Reconnect**; in the CLI run:

   ```sh
   uv run dapier connections connect <connection-id> --agent <agent-name>
   ```

   Updating a connection changes the requested-scope metadata, not an already
   issued token. A changed preset affects new connections only; edit existing
   connections explicitly. Check `dapier connections show <connection-id>`
   after consent to see the account and granted token scopes.

To remove scopes, update the list, revoke the old token, then reconnect so the
provider issues a token with the reduced access. Check workflows using the
connection before removing a scope:

```sh
uv run dapier connections edit <connection-id> --scopes <remaining-scope> [<scope> ...]
uv run dapier connections revoke <connection-id>
uv run dapier connections connect <connection-id> --agent <agent-name>
```

The console's **Revoke** action has the same effect as `connections revoke`.
Revocation attempts provider-side revocation where supported and clears the
stored Dapier token. Keep the identity scopes required for provider account
verification. For the current required scopes by provider, see its guide.

## Where scope behavior lives in the repository

| Change | Update |
|--------|--------|
| Default scopes for newly created connections | `CONNECT_SERVICES` in [`src/web/js/views/connections.js`](../../src/web/js/views/connections.js), pinned to [`src/dapier/connections/services.py`](../../src/dapier/connections/services.py) |
| Scopes on an existing connection | Console **Connectors → Edit**, or `uv run dapier connections edit <connection-id> --scopes ...` (`connections edit` also updates scopes) |
| Scopes allowed by Google consent | Google Cloud project `dtcdev-click` → **Google Auth Platform → Data access** |
| Scopes allowed by Dropbox | Dropbox App Console → app **Permissions**; also check Full Dropbox vs App Folder access |
| Scopes allowed by Zoom | Zoom App Marketplace → OAuth app **Scopes** |
| Scope validation, identity scopes, and provider consent behavior | [`src/dapier/connections/records.py`](../../src/dapier/connections/records.py) and [`src/dapier/connections/providers/oauth_providers.py`](../../src/dapier/connections/providers/oauth_providers.py) |
| Dropbox file access required by the invoice workflow | [`workflows/dropbox_on_upload.yaml`](../../workflows/dropbox_on_upload.yaml) and its Dropbox actions |

After changing existing connection scopes, reconnect it. To reduce access,
revoke and reconnect as above. Dropbox's [get current account endpoint](https://docs.dropboxapi.com/dropbox-api/api-reference/user-endpoints/users/get-current-account)
requires `account_info.read` for identity verification. Adding an app
permission does not change tokens already issued. Google's production apps
may need review for sensitive or restricted scopes; see the
[Google guide](../../plugins/google/README.md#google-oauth-publishing-status).

## Expected-account binding (optional)

Every OAuth connection is bound to the account that first consented. To pin a
connection to a known account up front, set `expected_account_id` through the
API/CLI import path. Consent from another account is rejected with 409 before
the connection is marked connected. The binding is immutable afterward;
pointing the connection at another account requires an explicit binding
replacement on reconnect.

## CLI parity, grants, and lifecycle

Every console action is reachable from the `dapier` CLI through the same
operator-gated `/api/agent/*` API:

```sh
# Consent flow (opens a browser and waits for consent)
uv run dapier connections connect google-calendar --agent <agent-name>

# Import a refresh token issued by another OAuth client
uv run dapier connections import youtube-personal \
  --provider youtube \
  --authorized-user-file refresh-token.json \
  --expected-account UCxxxxxxxx \
  --scopes https://www.googleapis.com/auth/youtube.readonly

# Inspect and revoke
uv run dapier connections list
uv run dapier connections show youtube
uv run dapier connections revoke youtube-personal
```

`import` with `--client-id` and `--client-secret-file` is only needed for
refresh tokens issued by a different client than the shared one. Grant an
agent access before using a connection:

```sh
uv run dapier grants save grant.json
# {"connection_id":"youtube","subject":"subject-9","agent":"buildcamp","operations":["read","write"]}
uv run dapier token exec youtube --agent buildcamp -- yt-dlp --version
```

Connection statuses:

- **awaiting consent** — created, but provider consent is incomplete. Click
  **Connect** or rerun `connections connect`; the authorization URL expires
  after 10 minutes.
- **connected** — verified against the provider and usable.
- **expired — needs refresh** — the refresh token no longer works (for
  example, it was revoked, the password changed, or Google requires consent
  again). Reconnect and approve again.

**Edit** can rename a connection, change requested scopes (except Slack, whose
scopes are carried in its token), or replace a Slack token. Tokens are
write-only; Dapier never displays or returns their values.

### Dropbox files forwarded to DataOps

The DataOps action stages Dropbox bytes with their SHA256 and submits the
supported `attachment` document kind. `recipient_route` selects a configured
DataOps route and defaults to `invoice` for Dropbox events. This setting is
available in the designer and in workflow YAML saved through the CLI/API.
DataOps owns invoice processing and review after accepting the document.

### Trigger retry state and cleanup

Authenticated webhook retry claims and poll deletion use the cursor table.
The API role can write only `poll#*`, `seen#poll#*` and `seen#hook#*` keys.
Poll deletion retains its trigger record when cursor cleanup fails, so the
operator can retry deletion after fixing the failure.

Webhook request-ID claims initialize their DynamoDB parent map atomically
before inserting a nested claim; this preserves other IDs in the same scope.
Deleting a hook also removes its retry-state scope.


Generic webhook intake parses JSON and `application/x-www-form-urlencoded`
fields, preserving field case and blank values. Repeated form keys become
arrays. A scalar `request_id` configured as `dedupe_path` identifies the same
retry across JSON and form encoding; without that field, deliveries remain
separate. HTTP 202 acknowledges intake, not downstream workflow completion.

Signed outbound webhooks support `payload_format: text` for exact templated XML
or text bytes, `content_type`, `signature_algorithm: sha1|sha256`, and
`signature_header`. Defaults preserve JSON and X-Dapier-Signature SHA256.
`secret_id_env` resolves a Secrets Manager ID on the server; neither the signing
secret nor the signature is returned to the operator. YouTube WebSub callback
validation rejects malformed XML or incomplete video identities before queuing
any entries. Empty feeds retain their no-op behavior.
