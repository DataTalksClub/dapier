# Posting to Slack from the CLI

`dapier slack post` and `dapier slack attach-image` post as the bot of a
stored Slack connection. The default connection is `slack` (the Au-Tomator
bot). The token stays server-side: the CLI calls `/api/agent/slack/*` and
never sees it.

## Text post

```bash
dapier slack post --channel C0123TEST --text "Hello"
dapier slack post --channel my-test-channel --text-file msg.md --thread 1800000000.000100
dapier slack post --channel C0123TEST --text "x" --dry-run   # access + scopes, posts nothing
```

The dry run reports `is_member`, the token's `scopes`, and `files_write`.

## Post with an image

```bash
dapier slack post --channel C0123TEST --text-file msg.md --image shot.png [--thread TS]
```

This uses Slack's files.uploadV2 flow:

1. `POST /api/agent/slack/image-upload-url` `{filename, length}`. The API
   calls `files.getUploadURLExternal` and returns `{file_id, upload_url}`.
   The upload URL is a Slack presigned URL that carries no token.
2. The CLI POSTs the bytes to that URL. It accepts only `https://files.slack.com`
   and never prints the URL. The bytes never pass through Lambda, so the
   only size limit is Slack's 1 GB.
3. `POST /api/agent/slack/post` `{channel, text, file_id, thread_ts?}`. The
   API calls `files.completeUploadExternal` with `initial_comment` set to the
   text. The image and the text arrive as one message. The API then looks up
   the new message's `ts` in the channel history or thread replies.

The output is `{ok, channel, ts, thread_ts, file: {id, name, title, permalink}}`.
Slack shares an upload asynchronously. If the `ts` does not appear within
about 9 seconds, the output has `ts: null` and a `ts_error`, and the CLI
exits 1. The message was still posted.

`--image` with `--dry-run` checks the local file and the `files:write`
scope. It uploads nothing.

## Attach an image to an existing message

Slack cannot add a file to a message that is already posted, so
`attach-image` has two modes:

```bash
# Default: the image goes into the message's thread as a reply.
dapier slack attach-image --channel C0123TEST --ts 1800000000.000100 --image shot.png [--text "caption"]

# --replace: repost text + image in the same place, then delete the old message.
dapier slack attach-image --channel C0123TEST --ts 1800000000.000100 --image shot.png --replace [--text "new text"]
```

`--replace` returns `{mode: "replace", ts: <new ts>, deleted_ts: <old ts>}`.
The new message gets a new `ts`, so update any links to the old one. Rules:

- Only a message posted by this connection's bot or user can be replaced.
  Other messages are refused with 403.
- A message that already has files, or a thread root with replies, is
  refused with 409, because a repost would drop the files or orphan the
  thread. Use the default thread-reply mode for these.
- A thread reply is reposted into the same thread.
- The text defaults to the original message text. `--text` or `--text-file`
  overrides it.
- The new message is posted first and the old one is deleted after. If the
  new `ts` cannot be confirmed, the old message is not deleted and the
  command fails, saying so.

## Errors

Errors fail loudly with a non-zero exit. There is no fallback to a
text-only post.

| Slack error | Message |
| --- | --- |
| `missing_scope` | names the needed scope and the scopes the token has (HTTP 403) |
| `not_in_channel` | the bot is not a member: invite it to the channel first |
| `channel_not_found` | channel not found or not visible to the bot |
| `file_too_large`, `invalid_length`, local file > 1 GB | file too big (HTTP 413) |
| a file that is not an image, or an empty file | refused before anything reaches Slack |

## Scopes

Image posts need the bot scope `files:write`, in addition to `chat:write`.
The ts lookup and `--replace` read history (`channels:history`, and
`groups:history`/`im:history` for private channels and DMs). Deleting uses
`chat:write`. Check the live token with `--dry-run`. If `files_write` is
false, add the scope to the Slack app (OAuth & Permissions → Bot Token
Scopes) and reinstall the app to the workspace. The bot token usually stays
the same. If Slack shows a new one, re-import it with `dapier connections import`.
