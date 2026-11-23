# Reading Slack attachments from the CLI

Read-only access to files attached to Slack messages, through a stored Slack
connection. The token stays server-side: the CLI calls `/api/agent/slack/*`,
and neither the token nor Slack's private file URLs ever reach the CLI,
its output, or the logs.

## 1. Find the file

`dapier slack history`, `dapier slack replies` and `dapier slack search`
return a `files` list on every message (empty when there are none):

```json
{"ts": "1800000000.000100", "text": "…", "files": [
  {"id": "F0123ABCD", "name": "report.pdf", "title": "report", "mimetype": "application/pdf",
   "filetype": "pdf", "pretty_type": "PDF", "size": 48213, "mode": "hosted",
   "is_external": false}]}
```

Images also carry `original_w` and `original_h`. URLs, thumbnails, previews and
the uploader are left out on purpose.

Use `replies` on the thread root to see which message carries a file: the root
comes first. Search results are not always attributed to the right message in a
thread, so check with `replies` before relying on them.

```bash
dapier slack replies --channel C0123TEST --thread-ts 1800000000.000100
```

## 2. Inspect or download it

```bash
# Metadata, and whether the API can download it (no URLs)
dapier slack file-info F0123ABCD --channel C0123TEST --ts 1800000000.000100

# Write it to disk; prints metadata, bytes, sha256 and path, never the content
dapier slack download F0123ABCD --channel C0123TEST --ts 1800000000.000100 -o ./downloads/
```

- `--channel` + `--ts` name the message that carries the file. The API finds
  the file on that message. This needs only history access to the channel.
- Without them the API uses `files.info`, which needs the `files:read` scope.
- `-o` takes a file path or an existing directory. The default is
  `./<file name>`. Only the basename of Slack's file name is used. An existing
  file is not overwritten unless you pass `--force`.
- The CLI checks the byte count and sha256 before it writes anything.

The default connection is `slack` (the Au-Tomator bot). For a channel the bot
cannot read (a private channel, or one it is not a member of), pass
`--connection slack-alexey`.

## Limits and safety

- Only `https://files.slack.com` is fetched. Redirects are followed only
  within that host, and the token is never sent anywhere else.
- Files over 4 MiB are refused with HTTP 413. The API returns the bytes as
  base64 JSON, which must fit the 6 MB Lambda/HTTP API response limit.
- External files (such as Google Drive links), deleted (`tombstone`) files and
  files hidden by the plan limit have no Slack-hosted content. `file-info`
  reports `downloadable: false` with the reason.
- Both routes are operator-gated (`slack.file_info`, `slack.file_download`)
  and audited, like the other `/api/agent/slack/*` routes.

## Scopes

| Step | Needs |
| --- | --- |
| `files` on history/replies | `channels:history` (`groups:history`, `im:history`, `mpim:history` for private channels and DMs) |
| `file-info`/`download` with `--channel`/`--ts` | the same history scope |
| `file-info`/`download` without them | `files:read` |
| fetching the content | Slack documents `files:read` for downloading file content |

Missing access fails with HTTP 403 (CLI exit code 4). The error names the
missing scope, says whether the token has `files:read`, and suggests
`--channel`/`--ts` or another connection. Dapier never asks for new scopes by
itself. Adding `files:read` means changing the Slack app's scopes and
reinstalling the app. That is an operator decision.

## API

| Route | Query |
| --- | --- |
| `GET /api/agent/slack/file` | `file_id`, optional `channel` + `ts`, `connection_id` (default `slack`) |
| `GET /api/agent/slack/file/content` | the same; the response adds `bytes`, `sha256`, `content_base64` |
