"""YouTube connector: channel, playlist, and playlist-video discovery, the
find-video and upload-video actions, the YouTube identity check, and the
"new video" poll source behind the trigger chip's sample pull.

YouTube connections are Google OAuth connections (provider "youtube"); the
discovery and health-check runners delegate to the shared provider layer
(``connections.discovery``), which hits the YouTube Data API with the
connection's own token.
"""
from ..connections import discovery as provider
from ..engine.actions.youtube import (
    run_youtube_add_to_playlist,
    run_youtube_create_playlist,
    run_youtube_find_playlist_items,
    run_youtube_find_video,
    run_youtube_remove_from_playlist,
    run_youtube_update_video,
    run_youtube_upload_video,
    run_youtube_subscription_status,
)
from ..triggers.poll_sources import PollSource, register_source
from .registry import (
    Action,
    ConnectionTest,
    Discovery,
    register,
    register_connection_test,
    register_discovery,
)

register(Action(
    type="youtube_subscription_status",
    label="YouTube: subscription status",
    description="Read authenticated WebSub lease state using the existing server-held secret; returns only state, expiry, topic and callback.",
    icon="youtube",
    run=lambda action, event, workflow_id, steps=None: run_youtube_subscription_status(action, event, steps=steps),
    required=frozenset({"channel_id"}),
    optional=frozenset(),
    fields=({"key": "channel_id", "label": "Channel ID", "required": True},),
))

register(Action(
    type="youtube_find_video",
    label="YouTube",
    description="Find the top videos for a search query (YouTube Data API search.list)",
    icon="youtube",
    run=lambda action, event, workflow_id, steps=None: run_youtube_find_video(
        action, event, steps=steps),
    required=frozenset({"connection_id", "query"}),
    optional=frozenset(),
    fields=(
        {"key": "connection_id", "label": "Connection ID", "placeholder": "youtube",
         "required": True},
        {"key": "query", "label": "Search query", "required": True,
         "placeholder": "DataTalks kubernetes"},
    ),
))

register(Action(
    type="youtube_find_playlist_items",
    label="YouTube: playlist videos",
    description="List the videos in a playlist, newest first (Find Playlist Videos)",
    icon="youtube",
    run=lambda action, event, workflow_id, steps=None: run_youtube_find_playlist_items(
        action, event, steps=steps),
    required=frozenset({"connection_id", "playlist_id"}),
    optional=frozenset(),
    fields=(
        {"key": "connection_id", "label": "Connection ID", "placeholder": "youtube",
         "required": True},
        {"key": "playlist_id", "label": "Playlist ID",
         "discover": {"resource": "youtube.playlists"}},
    ),
))

register(Action(
    type="youtube_upload_video",
    label="Upload video",
    description=("Upload one video to the connection's YouTube channel (YouTube Data "
                 "API videos.insert, multipart; Zapier's Upload Video). Content comes "
                 "from exactly one of source_url (a download URL), source_s3 "
                 "{bucket, key} (a staged object, e.g. dropbox_read_file's output), or "
                 "inline content; bytes stage in memory, so keep sources modest "
                 "(~100 MB ceiling). Output: {video_id, title, privacy_status, "
                 "upload_status, url}. The connection's OAuth grant needs the "
                 "youtube.upload scope (read-only tokens get HTTP 403)."),
    icon="youtube",
    run=lambda action, event, workflow_id, steps=None: run_youtube_upload_video(
        action, event, steps=steps),
    required=frozenset({"connection_id", "title"}),
    optional=frozenset({"source_url", "source_s3", "content", "description",
                        "category_id", "privacy_status", "tags"}),
    fields=(
        {"key": "connection_id", "label": "Connection ID", "placeholder": "youtube",
         "required": True},
        {"key": "title", "label": "Title", "required": True,
         "placeholder": "Deploying dapier: a walkthrough",
         "help": "The video's title on YouTube"},
        {"key": "source_url", "label": "Source URL", "type": "url",
         "placeholder": "https://example.test/talk.mp4",
         "help": ("One content source: a download URL — or source_s3 {bucket, key} "
                  "for a staged object, or inline content")},
        {"key": "content", "label": "Content",
         "help": "Inline text content — takes templates, e.g. {trigger.text}"},
        {"key": "description", "label": "Description",
         "help": "Shown under the video — takes templates"},
        {"key": "tags", "label": "Tags",
         "help": "Comma-separated tags, e.g. devops, kubernetes"},
        {"key": "category_id", "label": "Category ID",
         "help": "YouTube category id (22 is People & Blogs); validated by YouTube"},
        {"key": "privacy_status", "label": "Privacy", "type": "select",
         "options": ["public", "unlisted", "private"], "default": "unlisted",
         "help": "Unlisted is the default, like Zapier's Upload Video"},
    ),
))

register(Action(
    type="youtube_add_to_playlist",
    label="YouTube: add to playlist",
    description=("Add one video to a playlist the connection can edit "
                 "(playlistItems.insert; Zapier's Add Video to Playlist). "
                 "Output: {playlist_id, video_id, playlist_item_id, title, "
                 "position}. A video already in the playlist is YouTube's "
                 "videoAlreadyInPlaylist error, not a silent duplicate."),
    icon="youtube",
    run=lambda action, event, workflow_id, steps=None: run_youtube_add_to_playlist(
        action, event, steps=steps),
    required=frozenset({"connection_id", "playlist_id", "video_id"}),
    optional=frozenset(),
    fields=(
        {"key": "connection_id", "label": "Connection ID", "placeholder": "youtube",
         "required": True},
        {"key": "playlist_id", "label": "Playlist ID", "required": True,
         "discover": {"resource": "youtube.playlists"}},
        {"key": "video_id", "label": "Video ID", "required": True,
         "placeholder": "{trigger.video_id}",
         "discover": {"resource": "youtube.videos"},
         "help": "The video to add — {trigger.video_id} from a youtube trigger, "
                 "or a found video's id"},
    ),
))

register(Action(
    type="youtube_remove_from_playlist",
    label="YouTube: remove from playlist",
    description=("Remove one item from a playlist the connection can edit "
                 "(playlistItems.delete; Zapier's Remove Video from Playlist). "
                 "The playlist_item_id comes from youtube_add_to_playlist's "
                 "output or a youtube_find_playlist_items listing — not the "
                 "video id. An item already gone is {removed: false}, not an "
                 "error. Output: {removed, playlist_item_id}. playlistItems."
                 "delete needs the youtube.force-ssl scope (see "
                 "docs/connectors/google.md)."),
    icon="youtube",
    run=lambda action, event, workflow_id, steps=None: (
        run_youtube_remove_from_playlist(action, event, steps=steps)),
    required=frozenset({"connection_id", "playlist_item_id"}),
    optional=frozenset(),
    fields=(
        {"key": "connection_id", "label": "Connection ID", "placeholder": "youtube",
         "required": True},
        {"key": "playlist_item_id", "label": "Playlist item ID", "required": True,
         "placeholder": "{steps.add.output.playlist_item_id}",
         "help": "The playlist item to remove — youtube_add_to_playlist's "
                 "playlist_item_id output, or an item id from a "
                 "youtube_find_playlist_items listing"},
    ),
))

register(Action(
    type="youtube_create_playlist",
    label="YouTube: create playlist",
    description=("Create an empty playlist on the connection's channel "
                 "(playlists.insert, part=snippet,status; Zapier's Create "
                 "Playlist). Output: {playlist_id, title, url} — the id "
                 "chains into youtube_add_to_playlist."),
    icon="youtube",
    run=lambda action, event, workflow_id, steps=None: run_youtube_create_playlist(
        action, event, steps=steps),
    required=frozenset({"connection_id", "title"}),
    optional=frozenset({"description", "privacy_status"}),
    fields=(
        {"key": "connection_id", "label": "Connection ID", "placeholder": "youtube",
         "required": True},
        {"key": "title", "label": "Title", "required": True,
         "placeholder": "Deploying dapier: full episodes",
         "help": "The playlist's title on YouTube"},
        {"key": "description", "label": "Description",
         "help": "Shown on the playlist page — takes templates"},
        {"key": "privacy_status", "label": "Privacy", "type": "select",
         "options": ["private", "public", "unlisted"], "default": "private",
         "help": "Private is the default, like Zapier's Create Playlist"},
    ),
))

register(Action(
    type="youtube_update_video",
    label="YouTube: update video",
    description=("Update one video's title and description (videos.update, "
                 "part=snippet; Zapier's Update Video). YouTube replaces the "
                 "whole snippet on update, so pass category_id when the "
                 "video's category matters. Output: {updated: true, video_id, "
                 "title, description, category_id}."),
    icon="youtube",
    run=lambda action, event, workflow_id, steps=None: run_youtube_update_video(
        action, event, steps=steps),
    required=frozenset({"connection_id", "video_id", "title"}),
    optional=frozenset({"description", "category_id"}),
    fields=(
        {"key": "connection_id", "label": "Connection ID", "placeholder": "youtube",
         "required": True},
        {"key": "video_id", "label": "Video ID", "required": True,
         "placeholder": "{trigger.video_id}",
         "discover": {"resource": "youtube.videos"},
         "help": "The video to update — {trigger.video_id} from a youtube trigger"},
        {"key": "title", "label": "Title", "required": True,
         "help": "The video's new title — required: YouTube replaces the whole "
                 "snippet part"},
        {"key": "description", "label": "Description",
         "help": "The new description — takes templates; left out, YouTube clears it"},
        {"key": "category_id", "label": "Category ID",
         "help": "YouTube category id (22 is People & Blogs) — pass it when the "
                 "video's category matters, or the update clears it"},
    ),
))


def _run_channel(connection, params, *, transport=None):
    return provider.discover(connection, "channel", params, transport=transport)


register_discovery(Discovery(
    name="channel",
    connector="youtube",
    label="Channel",
    description="The connected YouTube channel",
    run=_run_channel,
))


def _run_playlists(connection, params, *, transport=None):
    return provider.discover(connection, "playlists", params, transport=transport)


register_discovery(Discovery(
    name="playlists",
    connector="youtube",
    label="Playlists",
    description="Playlists owned by the channel",
    run=_run_playlists,
))


def _run_playlist_items(connection, params, *, transport=None):
    return provider.discover(connection, "playlist_items", params, transport=transport)


register_discovery(Discovery(
    name="playlist_items",
    connector="youtube",
    label="Playlist videos",
    description="Videos in one playlist, newest first",
    params=({"key": "playlist_id", "label": "Playlist ID", "type": "text",
             "required": True},),
    run=_run_playlist_items,
))


def _run_videos(connection, params, *, transport=None):
    return provider.discover(connection, "videos", params, transport=transport)


register_discovery(Discovery(
    name="videos",
    connector="youtube",
    label="Channel videos",
    description="The connected channel's recent uploads, newest first",
    run=_run_videos,
))


def _run_test(connection, *, transport=None):
    return provider.test_connection(connection, transport=transport)


register_connection_test(ConnectionTest(connector="youtube", run=_run_test))


# --- poll source: "New Video" on the poll-trigger schedule --------------------
#
# video.published fires from PubSubHubbub (api.router._youtube) — but that
# needs a publicly reachable webhook endpoint verified with Google. The
# ``youtube.videos`` source is the no-webhook path: a stored poll trigger
# lists the channel's uploads playlist through the shared provider layer on
# the schedule machinery (triggers.poll_sources) and publishes the same
# ``youtube``/``video.published`` event in the notification's data shape, so
# a workflow matches the same chip either way. The first fire seeds the
# cursor without emitting: enabling a trigger must not fire the channel's
# existing uploads.


def _youtube_poll_validate(body):
    """Save-time fetch spec: the ``connection_id`` of the YouTube connection
    to poll as (required — the fetch refreshes its OAuth token), the
    optional ``channel_id`` (empty stores "the connection's own channel",
    resolved through channels.mine at fetch time), plus the fetch defaults
    every stored youtube poll carries."""
    from ..triggers.email_triggers import TriggerError

    body = body if isinstance(body, dict) else {}
    if not str(body.get("connection_id") or "").strip():
        raise TriggerError(
            "connection_id is required: pick the YouTube connection to poll as")
    return {
        "channel_id": str(body.get("channel_id") or "").strip(),
        "cursor_mode": "next_cursor",
        "id_path": "id",
        "url": "",
    }


def _youtube_poll_channel(connection, item, *, transport=None):
    """The channel to watch: the stored ``channel_id``, else the connection's
    own — the same channels.mine resolution the chip's live sample uses."""
    stored = str(item.get("channel_id") or "").strip()
    if stored:
        return stored
    channels = _run_channel(connection, {}, transport=transport)
    return next((channel.get("id") for channel in channels
                 if isinstance(channel, dict) and channel.get("id")), None)


def _youtube_poll_video(entry, channel_id):
    """One playlist entry flattened to the ``video.published`` notification's
    data shape (api.router._youtube): video identity, title, watch URL — so
    downstream templates read the same keys no matter which path fired.
    ``published`` rides along: the playlist listing's publishedAt, which keys
    the watermark (the webhook notification itself carries no publish time)."""
    video_id = str(entry.get("id") or "")
    return {
        "id": video_id,
        "video_id": video_id,
        "channel_id": channel_id,
        "title": entry.get("name"),
        "url": f"https://www.youtube.com/watch?v={video_id}",
        "published": str(entry.get("published") or ""),
    }


def _youtube_poll_fetch(item, cursor=None, *, transport=None):
    """One poll page as ``(items, next_cursor)`` for ``triggers.poll_sources``.

    Lists the channel's uploads playlist — the channel id with the ``UC``
    prefix swapped for ``UU``, the same swap ``_live_upload`` makes — through
    the shared provider layer with the connection record behind the stored
    ``connection_id``, so the OAuth token is refreshed exactly like the
    actions' calls. Items are the recent uploads in the notification's data
    shape (see ``_youtube_poll_video``).

    The cursor is the newest fired ``published`` (ISO Zulu, so text
    comparison orders it; the video id breaks ties). With no stored cursor —
    the first fire after enabling — the fetch only seeds the watermark at
    the playlist's newest upload and emits nothing: the channel's existing
    videos are history, not news. With a cursor, only uploads published
    strictly after it fire, oldest first, and the parked cursor is the newest
    fired ``published`` (the incoming one when nothing qualifies; ``fire``
    parks it only once the page drains). Raises ``RuntimeError`` on a failed
    fetch, like every poll source.
    """
    from ..engine.actions import base

    if not str(item.get("connection_id") or "").strip():
        raise RuntimeError("poll source 'youtube.videos' needs connection_id: "
                           "the YouTube connection to poll as")
    try:
        connection = base._connected_connection(item["connection_id"])
        channel_id = _youtube_poll_channel(connection, item, transport=transport)
    except ValueError as exc:  # covers the provider layer's DiscoveryError
        raise RuntimeError(f"youtube poll failed: {exc}") from None
    if not channel_id:
        raise RuntimeError("youtube poll failed: could not resolve the "
                           "connection's own channel; store a channel_id")
    # A YouTube channel id starts with UC; its uploads playlist is the same
    # id with UU (see _live_upload).
    try:
        entries = _run_playlist_items(connection, {"playlist_id": "UU" + channel_id[2:]},
                                      transport=transport)
    except ValueError as exc:
        raise RuntimeError(f"youtube poll failed: {exc}") from None
    videos = [_youtube_poll_video(entry, channel_id) for entry in entries
              if isinstance(entry, dict) and entry.get("id")]
    if cursor is None:
        # First fire: seed the watermark at the playlist's newest upload
        # (None on an empty channel) without emitting anything.
        return [], max((video["published"] for video in videos), default=None)
    watermark = str(cursor)
    fresh = sorted(
        (video for video in videos if video["published"] > watermark),
        key=lambda video: (video["published"], video["id"]))
    return fresh, (fresh[-1]["published"] if fresh else watermark)


def _youtube_poll_view(item):
    """The provider params ``public_view`` shows beside ``source``."""
    return {"channel_id": item.get("channel_id")}


register_source(PollSource(
    name="youtube.videos", connector="youtube", event="video.published",
    label="YouTube", validate=_youtube_poll_validate,
    fetch=_youtube_poll_fetch, view=_youtube_poll_view))


def _stored_youtube_poll(name):
    """The stored poll trigger named by ``event`` when it watches YouTube,
    or None. Poll ids cannot contain dots and event names do, so a per-event
    ask never matches a poll; a missing selector, unconfigured poll
    triggers, an unknown name and a non-youtube source (the generic poll
    connector owns those) fold together: the caller only distinguishes
    live-vs-fallback, so any storage hiccup folds too — sampling never
    raises for want of infrastructure (see docs/connector-coverage-audit.md)."""
    from ..triggers import poll_triggers

    name = str(name or "").strip().lower()
    if not name or "." in name:
        return None
    try:
        item = poll_triggers.get_item(name)
    except Exception:
        return None
    if not item or str(item.get("source") or "") != "youtube.videos":
        return None
    return item


# Older than any YouTube publishedAt: the chip's live sample pulls against
# this watermark, so the playlist's newest upload answers before the trigger
# is even past its first (seeding) fire.
_YOUTUBE_EPOCH_CURSOR = "0000-01-01T00:00:00Z"


# --- trigger discovery: playlist options and the trigger sample -----------------
#
# The chip declares video.published (the PubSubHubbub notification
# api.router._youtube publishes). The sample prefers live data — a stored
# youtube.videos poll's newest upload when ``event`` names one, else the
# connected channel's newest upload, via the playlist_items listing — and
# falls back to recorded history, then a documented notification, so the
# designer preview always renders (the slack connector's fetch chain).

from . import trigger_discovery  # noqa: E402
from .trigger_discovery import (  # noqa: E402
    DEFAULT_LIMIT,
    DiscoveryNotFound,
    TriggerDiscovery,
    options_from_registry,
    register_trigger_discovery,
)

SAMPLE_EVENT = "video.published"

# A documented PubSubHubbub notification for accounts with no live upload
# to pull (see api.router._youtube for the real delivery's normalization).
_SYNTHETIC_NOTIFICATION = {
    "video_id": "dQw4w9WgXcQ",
    "channel_id": "UCbW5IB0F8d1MpdW2AhifDzw",
    "title": "Deploying dapier: a walkthrough",
    "url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
}


def _fetch_playlist_options(event=None, connection_id=None, limit=DEFAULT_LIMIT):
    """Playlist options for the find-playlist-items action's playlist field
    (first connected YouTube connection when no id is named)."""
    return options_from_registry(
        "youtube.playlists", connection_id, limit, provider="youtube",
        option_of=lambda item: {"value": item.get("id"),
                                "label": item.get("name") or item.get("id")})


register_trigger_discovery(TriggerDiscovery(
    connector="youtube", label="YouTube", kind="options", resource="youtube.playlists",
    fetch=_fetch_playlist_options))


def _fetch_channel_options(event=None, connection_id=None, limit=DEFAULT_LIMIT):
    """The connected channel as an option (via the registry listing)."""
    return trigger_discovery.options_from_registry(
        "youtube.channel", connection_id, limit, provider="youtube",
        option_of=lambda item: {"value": item.get("id"),
                                "label": item.get("name") or item.get("id")})


register_trigger_discovery(TriggerDiscovery(
    connector="youtube", label="YouTube", kind="options", resource="youtube.channel",
    fetch=_fetch_channel_options))


def _fetch_playlist_item_options(event=None, connection_id=None, limit=DEFAULT_LIMIT):
    """Video options for one playlist (via the registry); the playlist id
    rides in ``event`` (trigger_discovery.listing_params)."""
    return trigger_discovery.options_from_registry(
        "youtube.playlist_items", connection_id, limit, provider="youtube",
        params=trigger_discovery.listing_params(event, ("playlist_id",)),
        option_of=lambda item: {"value": item.get("id"),
                                "label": item.get("name") or item.get("id")})


register_trigger_discovery(TriggerDiscovery(
    connector="youtube", label="YouTube", kind="options", resource="youtube.playlist_items",
    fetch=_fetch_playlist_item_options))


def _fetch_video_options(event=None, connection_id=None, limit=DEFAULT_LIMIT):
    """The channel's recent uploads as options (via the registry listing)."""
    return trigger_discovery.options_from_registry(
        "youtube.videos", connection_id, limit, provider="youtube",
        option_of=lambda item: {"value": item.get("id"),
                                "label": item.get("name") or item.get("id")})


register_trigger_discovery(TriggerDiscovery(
    connector="youtube", label="YouTube", kind="options", resource="youtube.videos",
    fetch=_fetch_video_options))


def _live_upload(connection, *, transport=None):
    """The connected channel's newest upload as a video.published data dict.

    The channel's uploads playlist is its id with the ``UC`` prefix swapped
    for ``UU``, so one playlist_items listing serves the newest video
    without a second feed API.
    """
    channels = provider.discover(connection, "channel", {}, transport=transport)
    channel_id = next((channel.get("id") for channel in channels
                       if isinstance(channel, dict) and channel.get("id")), None)
    if not channel_id:
        return None
    items = provider.discover(connection, "playlist_items",
                              {"playlist_id": "UU" + channel_id[2:]},
                              transport=transport)
    item = next((entry for entry in items
                 if isinstance(entry, dict) and entry.get("id")), None)
    if item is None:
        return None
    return {
        "video_id": item.get("id"),
        "channel_id": channel_id,
        "title": item.get("name"),
        "url": f"https://www.youtube.com/watch?v={item.get('id')}",
    }


def _fetch_sample(event=None, connection_id=None, limit=DEFAULT_LIMIT, transport=None):
    """The channel's newest upload live, else recorded history, else an example.

    An ``event`` naming a stored youtube.videos poll pulls live through the
    poll's own fetch against the epoch watermark — no stored cursor is read
    or advanced — before the webhook-side chain runs. No connected YouTube
    account, or a failed listing, falls through — the sample pull must
    answer with something a workflow author can build on.
    """
    from ..triggers import poll_triggers

    item = _stored_youtube_poll(event)
    if item is not None:
        try:
            videos, _next_cursor = _youtube_poll_fetch(item, _YOUTUBE_EPOCH_CURSOR)
            envelope = (poll_triggers.event_for(item, videos[-1])
                        if videos else None)
        except Exception:
            envelope = None
        if envelope is not None:
            return {
                "sample": trigger_discovery.as_sample(envelope),
                "source": "live",
                "connection_id": item.get("connection_id") or None,
            }
    try:
        connection = trigger_discovery.connected_connection("youtube", connection_id)
    except DiscoveryNotFound:
        connection = None
    if connection is not None:
        try:
            data = _live_upload(connection, transport=transport)
        except Exception:  # provider failures fall through, never 502 past a workable sample
            data = None
        if data is not None:
            return {"sample": trigger_discovery.as_sample({
                "connector": "youtube",
                "event": event or SAMPLE_EVENT,
                "source": connection["connection_id"],
                "data": data,
            }), "source": "live", "connection_id": connection["connection_id"]}
    found = trigger_discovery.history_sample("youtube", event=event)
    if found is not None:
        if event:
            found["event"] = event
        return {"sample": found, "source": "history", "connection_id": connection_id}
    return {"sample": trigger_discovery.synthetic_sample(
        "youtube", event or SAMPLE_EVENT, dict(_SYNTHETIC_NOTIFICATION)),
        "source": "synthetic", "connection_id": connection_id}


register_trigger_discovery(TriggerDiscovery(
    connector="youtube", label="YouTube", kind="sample", resource="",
    fetch=_fetch_sample))
