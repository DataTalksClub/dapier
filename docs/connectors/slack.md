# Slack connector

Slack does not use Dapier's OAuth client. Paste a Slack bot or user token;
Dapier validates it with `auth.test` before storing it write-only under the
connection's credential ID.

1. Create or select an app at [api.slack.com/apps](https://api.slack.com/apps).
2. Under **OAuth & Permissions**, grant the bot at least `chat:write`; add
   `channels:read` or `groups:read` if the workflow needs to inspect those
   channels.
3. Install the app to the workspace and copy the **Bot User OAuth Token**
   (`xoxb-…`). A user token (`xoxp-…`) also works.
4. Invite the bot to channels where it should post, for example
   `/invite @your-bot`.
5. In Dapier, open **Connectors → Slack → Create new**, paste the token, and
   save. The connection should show **connected** with the workspace identity.

A bad or revoked token is rejected before it is stored. Slack scopes are
carried by the token, so the requested-scopes editor is hidden for Slack.
Replace a token by editing the connection or importing a new token using the
CLI. See the [shared connector guide](README.md#cli-parity-grants-and-lifecycle)
for the common connection commands.
