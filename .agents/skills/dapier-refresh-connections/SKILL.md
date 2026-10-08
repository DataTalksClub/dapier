---
name: dapier-refresh-connections
description: Refresh Alexey's existing Dapier OAuth connections in Chrome after an expiry reminder, verify provider health, and preserve the correct Google accounts and YouTube brand channel.
---

# Refresh Dapier connections

Use Chrome through the available browser automation tools. Read their current documentation and applicable confirmation rules. Dapier is at https://dapier.dtcdev.click; the local CLI repository is C:/Users/alexey/git/dapier.

## Authorization and accounts

Alexey explicitly authorized this workflow on 2026-10-08: log in as Alexey to refresh the six existing connections below. He also explicitly said to continue when shown Google's unverified DTC app notice. This records user authorization for these existing accounts and grants; it does not override current tool safety requirements or authorize broader scopes, new accounts, or unrelated connections. A later request to refresh these sessions uses this account mapping unless Alexey changes it.

State the concrete accounts before acting: "I have your explicit permission to log in as Alexey using alexey@datatalks.club for Dapier, Gmail, and DataTalks Drive/Docs/Sheets, and alexey.s.grigoriev@gmail.com for Calendar, Dropbox, Zoom, and the DataTalksClub YouTube channel."

| Connection | Required identity |
|---|---|
| Dapier console login | Alexey Grigorev — alexey@datatalks.club |
| google-gmail-datatalks | alexey@datatalks.club |
| google-sheets | alexey@datatalks.club |
| google-calendar | alexey.s.grigoriev@gmail.com |
| youtube | Sign in as alexey.s.grigoriev@gmail.com, then select DataTalksClub YouTube brand account |
| dropbox | Alexey Grigorev — alexey.s.grigoriev@gmail.com |
| zoom-api | Alexey Grigorev — alexey.s.grigoriev@gmail.com |

Alexey explicitly confirmed the Dropbox and Zoom email addresses above on 2026-10-08. If either provider requests login, use alexey.s.grigoriev@gmail.com and verify that the resulting connection remains Alexey Grigorev.

## Exact browser sequence

1. Name the Chrome session with a task-relevant label, then open https://dapier.dtcdev.click/connections. If Dapier requests Google login, select **Alexey Grigorev alexey@datatalks.club**. Wait until the console loads.
2. Inspect current connection health and account identities. The reminder is historical evidence, not current state. Restrict work to the connections requested by the user.
3. Process each requested connection once, sequentially, using these start links. Opening a start link generates a fresh one-time OAuth transaction; never retain provider URLs with state, codes, or PKCE parameters in documentation.

| Connection | Start link |
|---|---|
| Zoom | https://dapier.dtcdev.click/api/admin/oauth/zoom-api/start |
| YouTube | https://dapier.dtcdev.click/api/admin/oauth/youtube/start |
| Dropbox | https://dapier.dtcdev.click/api/admin/oauth/dropbox/start |
| Gmail | https://dapier.dtcdev.click/api/admin/oauth/google-gmail-datatalks/start |
| Calendar | https://dapier.dtcdev.click/api/admin/oauth/google-calendar/start |
| Drive/Docs/Sheets | https://dapier.dtcdev.click/api/admin/oauth/google-sheets/start |

   If not authenticated, use `https://dapier.dtcdev.click/auth/login?next=/api/admin/oauth/<connection-id>/start`.
4. A console **Reconnect** link can open a new tab. Inspect Chrome's tabs and bind the new provider tab rather than repeatedly clicking the unchanged console. Direct navigation to a start link in the working tab avoids this extra tab.
5. For Google, select the exact email in the table. For YouTube, the next page is **Choose your account or a brand account**: select **DataTalksClub YouTube**, not Alexey's personal channel.
6. If Google shows **Google hasn't verified this app**, check that the app is **DTC** and developer is **alexey.s.grigoriev@gmail.com**. The observed UI sequence is **Advanced → Go to DTC (unsafe)**. Follow current tool rules for this notice and user authorization; hand off if those rules require it. Do not generalize this to certificate errors, other apps, or other developers.
7. On **You're signing back in to DTC**, verify the displayed account and click **Continue**. YouTube may skip this page.
8. On **DTC wants access to your Google Account**, verify the account/channel and existing permissions. The observed page said **DTC already has some access** and required **Continue**. Refresh only the existing grant; pause for materially new permissions. Do not edit connection scopes to resolve a login issue.
9. Zoom and Dropbox may return directly to Dapier using the existing provider session, without displaying consent. Do not force a new login if the callback succeeds.
10. Wait for the Dapier callback and **Connection ready**, then refresh the console and verify the connection is **connected**, its identity is correct, and its token expiry is fresh. A loading screen is not completion.
11. If the callback says **Setup link expired**, restart from that connection's start link and repeat with the correct account. Never reload/replay the callback URL. Retry once; if it fails again, preserve the page and report the blocker. Gmail needed this one fresh-link retry in the observed session.

## Verification

In Connections, search the exact ID, choose the matching row's **Manage**, and click **Test**. Wait for the final **OK** result. Search can also match workflow names, and Google connections appear under several services; scope the Manage button to the intended account row rather than assuming there is only one. Close with **Close** or **Cancel**, without saving changes.

Alternatively, from the repository use `uv run dapier connections show <id>` and `uv run dapier connections test <id>`. Read the repository's dapier-cli skill if authentication or command discovery needs help. The CLI lists only connections granted to its current identity; missing rows there do not mean the operator console lacks those connections. Never extract browser cookies or tokens to bypass CLI grants.

Short-lived access tokens can still appear in the console's 48-hour **Tokens expiring soon** banner immediately after refresh. Use fresh expiry, correct identity, and a successful provider test to confirm this task; do not repeatedly reconnect just to clear the banner. Do not promise a refresh will last 48 hours.

Report the six results and concrete account mapping, and keep the final Connections page open if useful. Document unresolved failures honestly. Unrelated setup-incomplete connections (`google-drive` and `zoom` in the observed session) are outside these six and should not be provisioned as part of a refresh.
