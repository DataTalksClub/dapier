# OAuth token factory in Dapier

## Outcome

Dapier owns named OAuth connections for multiple providers and accounts. An
operator signs in with the existing DataTalksClub (DTC) shared identity, grants
provider consent in Dapier, and assigns connections to agents. A CLI uses the
same DTC identity to obtain a usable, short-lived provider access token for an
authorized agent. Provider refresh tokens and client secrets stay in Dapier.

Initial connections:

| Connection ID | Provider account | Minimum scope | Consumer |
| --- | --- | --- | --- |
| `youtube-datatalksclub` | DataTalksClub channel `UCDvErgK0j5ur3aLgn6U-LqQ` | `https://www.googleapis.com/auth/youtube` for existing publishing tools | Zoomcamp publishing agents |
| `youtube-personal` | `@stolzenable` (resolve and bind its channel ID during connection) | `https://www.googleapis.com/auth/youtube.upload` | AI Shipping Labs buildcamp upload agent ([website #1680](https://github.com/AI-Shipping-Labs/website/issues/1680)) |

These are distinct connection records and distinct secret versions, even if
they use the same Google OAuth client. Selecting a connection is mandatory for
any token request or write operation. No implicit "current YouTube account".
Additional providers (for example Dropbox) use the same lifecycle and access
contract, with provider-specific adapters for consent parameters, refresh,
identity verification, and token response details.

## Existing Dapier foundation

- `src/admin.py` already implements DTC OIDC browser login through
  `AUTH_BASE_URL`/`AUTH_ISSUER`, validates ID tokens, and issues a Dapier session.
- The same module has OAuth start/callback handlers, provider endpoints for
  YouTube and Dropbox, connection metadata in `CONNECTIONS_TABLE`, and
  `dapier/oauth/<connection_id>` secrets in Secrets Manager.
- `src/web/` already has a Connections view. `template.yaml` deploys the admin
  API and callback in the sandbox account.

Extend these components. Do not create a second auth system, separate public
OAuth callback Lambda, or a new standalone token-service repository. Separate
modules inside Dapier are welcome so `src/admin.py` does not grow indefinitely.

## Identity and authorization

1. Human operators use DTC shared auth. Keep the existing web OIDC flow. Add
   an explicit operator authorization check after authentication; a valid DTC
   account alone does not grant credential administration.
2. The CLI uses the same DTC issuer with an authorization-code + PKCE flow and
   a loopback callback. Register a dedicated public/native CLI client and its
   allowed localhost redirect URIs in the shared-auth stack. Do not copy a web
   session cookie or store a DTC password in the CLI. Verify issuer, audience,
   nonce/state, expiry, and the operator's subject on the Dapier API.
3. Store connection grants keyed by stable DTC subject and agent identity, with
   explicit connection ID and allowed operation (`use`, `connect`, `admin`).
   Deny by default. A caller cannot name an arbitrary agent to gain its grants.
4. For a local agent running under the operator's CLI session, allow a
   short-lived, connection-scoped delegation. A headless agent must use a
   separately enrolled machine identity and its own grants; never ship the
   operator's refresh credential to a server. Specify enrollment, revocation,
   and expiry before enabling headless access.
5. Dapier records who connected, used, refreshed, rotated, and revoked a
   connection. Audit records contain IDs and outcomes, never tokens, auth codes,
   client secrets, or full authorization URLs.

## Connection and token model

Connection metadata in DynamoDB includes `connection_id`, provider,
display name, owner DTC subject, requested/granted scopes, expected and
verified provider account ID, status, secret ARN, timestamps, and a version.
The provider account ID is immutable after first verified consent unless an
operator explicitly replaces the connection. In particular, binding
`youtube-personal` to the DataTalksClub channel must fail.

Secrets Manager stores the OAuth client credential, refresh token, and
provider-specific durable metadata under one versioned secret per connection.
Short-lived access tokens are renewed as needed; they do not need separate
durable backups. DynamoDB does not contain bearer credentials. Preserve a valid
refresh token when a refresh response omits `refresh_token`. Make connection
edits and token refreshes safe under concurrent use: do not overwrite a newer
secret with stale state, and do not lose a rotated refresh token. Redact
provider error bodies before logging or returning them.

Provider adapters define authorization URL, token URL, extra consent fields,
scope normalization, refresh method, account identity check, and revocation if
supported. OAuth authorization-code flows use PKCE S256, exact state matching,
fixed registered redirect URIs, bounded state lifetime, and single-use
callbacks. Validate the callback's connection ID and operator identity.
Never derive a redirect URI from an untrusted forwarded-host header.

For YouTube, verify the authenticated channel ID with `channels.list(mine=true)`
before marking a connection ready or returning a token. If the upload-only
scope cannot make that check, fail closed and document the minimum extra scope
or a separate verification flow; do not silently widen `youtube-personal` to
full read/write access. Verify the chosen Brand Account in the consent flow.
The CLI's status output shows connection ID, provider, verified channel
title/ID, scopes, and health, but no credentials.

## CLI and API contract

Provide a `dapier` CLI (packaged from this repo) with commands along these
lines; exact flags may follow the project's CLI conventions:

```text
dapier auth login
dapier auth status
dapier auth logout
dapier connections list
dapier connections show youtube-personal
dapier connections connect youtube-personal
dapier token exec youtube-personal --agent buildcamp-uploader -- <command>
dapier token write youtube-personal --agent buildcamp-uploader --output <private-file>
```

`token exec` starts the child process with a fresh provider access token in a
documented environment variable and clears it when the process exits. It
never prints the token. `token write` creates a private file (mode 0600 on
POSIX; owner-only ACL on Windows), refuses to overwrite by default, and
returns only its path. Access tokens should have the provider's normal short
lifetime. Neither command returns refresh tokens or client secrets. The CLI
must not place credentials in command-line arguments, shell history, debug
output, an `.env` file, or the deployed AI Shipping Labs website.

The API authenticates the CLI using DTC-issued identity, checks the
connection grant, refreshes the provider token when necessary, and returns
only the access token, expiry, scope, and verified provider account ID over
TLS with `Cache-Control: no-store`. The CLI checks the returned account ID
against the requested connection before handing the token to a process.
The token endpoint is rate-limited and audited. Browser admin endpoints keep
CSRF protection and an explicit operator authorization check.

The SDK or CLI integration for an agent must require a connection ID and an
expected provider account ID. A publishing action fails before any upload if
the returned account ID does not match. The existing
`youtube-manager-agent/.youtube/token.json` remains the DataTalksClub local
credential until its tools are explicitly migrated; do not replace it with the
personal token. A compatibility adapter may materialize a Google authorized
user JSON file for local tools, but must keep the per-connection separation and
private-file rules.

## Storage, deployment, and migration

Use Dapier's existing Lambda/API Gateway deployment, DynamoDB table, and
Secrets Manager boundary. Narrow Lambda IAM from the current wildcard
`dapier/oauth/*` management permission where practical, and grant CLI callers
access through the Dapier API rather than direct Secrets Manager reads. Do not
put secret values in CloudFormation, Git, Terraform state, or CI output.

The currently working DataTalksClub OAuth credential is in
`youtube-manager-agent/.youtube/` and backed up in the main account's
`zoomcamp-ops/youtube/{client-secret,token}` secrets. Import it into Dapier as
`youtube-datatalksclub` through a one-time operator command that transfers
bytes without logging them, verifies the DataTalksClub channel ID, and checks
that refresh works. Keep the existing backup and Hetzner copy until the new
connection and consumers have been verified. Do not change or delete either
backup as part of initial deployment.

Create `youtube-personal` through a fresh consent selecting `@stolzenable`.
Do not copy the DataTalksClub token or assume that the Google account picker
selected the intended channel. Nothing in the AI Shipping Labs production
website needs this credential; the buildcamp migration is an operator job.
There is an interim local upload-only cache in
`youtube-manager-agent/.youtube/personal/token.json`; its channel identity has
not been verified because `youtube.upload` cannot call `channels.list`. Treat
it as untrusted input for migration, and complete verified consent in Dapier.

Do not automate the 107-video upload as part of this work. Confirm the current
YouTube API upload quota and the unaudited-project private-video restriction
before choosing an API uploader. Pilot one unlisted video and its gated embed
as called for in [#1680](https://github.com/AI-Shipping-Labs/website/issues/1680).

## Acceptance checks

1. Two YouTube connections can coexist; list/status makes their provider
   identities and scopes distinguishable without exposing secrets.
2. DTC shared-auth login succeeds in browser and CLI; unauthenticated users,
   ordinary DTC users, expired sessions, and agents without a grant cannot
   request a token or administer a connection.
3. Connecting `youtube-personal` with the DataTalksClub account, or vice versa,
   is rejected before the connection is marked ready. Reconnect and token
   refresh cannot silently change the bound provider account.
4. `token exec` for one granted connection supplies a fresh access token only
   to its child process; requests for a different connection are denied. Token
   values do not appear in logs, CLI status, exceptions, or process arguments.
5. Expired access tokens refresh, refresh-token rotation is preserved, and
   concurrent requests cannot regress the stored secret version. Revocation
   makes subsequent token requests fail.
6. Callback tests cover state replay, wrong operator, changed host, bad code,
   missing/changed scopes, and provider errors. IAM/API tests cover main and
   sandbox deployment identities and connection-specific grants.
7. The DTC credential import is verified against channel
   `UCDvErgK0j5ur3aLgn6U-LqQ`; existing YouTube manager and Zoomcamp flows
   continue to work. No personal token is added to their default paths.

## Delivery order

1. Extract Dapier's existing OAuth code into tested auth, provider, connection,
   and secret-store modules; preserve current web console behavior.
2. Add channel binding, refresh lifecycle, per-connection authorization, and
   migration/import tooling.
3. Add the DTC-authenticated CLI and token delivery contract.
4. Enroll the two YouTube connections, verify end-to-end access, then migrate
   individual consumers deliberately.
