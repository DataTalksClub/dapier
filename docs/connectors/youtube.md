# YouTube connector

YouTube is a Google OAuth connection (provider `youtube`) on the shared
Google client. The [Google guide](google.md) is the source of truth for:

- creating and connecting the channel — OAuth consent with
  `youtube.readonly`, channel verification, and the Brand Account fallback
  when no channel is found ([Google setup](google.md#google-cloud-scope-configuration)
  and [account connections](google.md#calendar-youtube-and-other-accounts));
- the `video.published` push trigger — YouTube's PubSubHubbub feed on the
  shared `/hooks/youtube` callback, the hub secret, and the five-day
  renewal schedule ([YouTube video push](google.md#youtube-video-push-websub));
- the **Upload video** action and its scope caveat: uploading needs
  `https://www.googleapis.com/auth/youtube.upload`, and Dapier connections
  request only `youtube.readonly` today — a read-only token fails the
  upload with HTTP 403 ([YouTube upload video](google.md#youtube-upload-video)).

This page is the YouTube-specific rest: triggers without webhooks, the
action catalog, and discovery.

## Triggers

| Trigger | Kind | Notes |
| --- | --- | --- |
| `youtube` / `video.published` | hook (push) | fires from the PubSubHubbub notification; setup lives in the [Google guide](google.md#youtube-video-push-websub). The trigger stores a `channel_id` filter; disabling or deleting never unsubscribes — the shared callback means other watchers keep the subscription, and the renewal schedule releases released channels by lapsing. |
| `youtube.videos` poll | poll (no webhook) | the no-push path: a stored poll trigger lists the watched channel's uploads playlist on the schedule and publishes the same `youtube` / `video.published` event, so workflows match the same chip either way |

The poll trigger (`dapier polls save`) needs the `connection_id` of the
YouTube connection to poll as (its OAuth token is refreshed like the
actions' calls); an empty `channel_id` resolves to the connection's own
channel at fetch time:

```json
{
  "name": "channel-uploads",
  "source": "youtube.videos",
  "expression": "rate(1 hour)",
  "connection_id": "youtube",
  "flow": "channel-uploads"
}
```

Each fire lists recent uploads (the channel id with `UC` swapped for `UU`
— the uploads playlist) and publishes videos published strictly after the
stored watermark, in the notification's data shape: `{id, video_id,
channel_id, title, url, published}`. `published` rides along only from the
poll — the webhook notification itself carries no publish time. The first
fire seeds the watermark without emitting.

## Actions

All actions take `connection_id`.

| action | YouTube Data API | notes |
| --- | --- | --- |
| `youtube_find_video` | `search.list` | top videos for a search query |
| `youtube_find_playlist_items` | `playlistItems.list` | one playlist's videos, newest first; `playlist_id` from the playlists discovery |
| `youtube_upload_video` | `videos.insert` (multipart) | needs the `youtube.upload` scope ([caveat](google.md#youtube-upload-video)); bytes from exactly one of `source_url`, staged `source_s3 {bucket, key}`, or inline `content`; `privacy_status` defaults to unlisted; ~100 MB ceiling |
| `youtube_add_to_playlist` | `playlistItems.insert` | adds one video to an editable playlist; an already-present video surfaces YouTube's `videoAlreadyInPlaylist` error rather than duplicating silently |
| `youtube_update_video` | `videos.update` (snippet) | title required — YouTube replaces the whole snippet, so pass `category_id` (and the description, which an omitted value clears) when they matter |

## Discovery

YouTube connections list live resources over
`dapier connections discover <connection> [resource]` and the console/CLI
pickers: `channel` (the connected channel), `playlists`, `playlist_items`
(one playlist's videos, needs `playlist_id`), and `videos` (the channel's
recent uploads, newest first).

## Gotchas

- Connection setup, scopes, and the WebSub subscription lifecycle are
  [Google-guide](google.md) territory — this page deliberately does not
  repeat them.
- The push and poll paths publish the same event name; a workflow filter on
  `channel_id` works for both, but only the poll envelope carries
  `published`.
- A youtube poll without a stored `channel_id` resolves the connection's
  own channel through `channels().mine` at fetch time — point it at a
  different channel explicitly when the connection can see more than one.
