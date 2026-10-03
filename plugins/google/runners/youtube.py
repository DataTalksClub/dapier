"""youtube_find_video / youtube_find_playlist_items: search YouTube and list
playlist videos through a Google connection. The write-back actions run on
the same connection: ``youtube_upload_video`` puts one video onto the channel
(Data API multipart videos.insert), ``youtube_add_to_playlist`` files one
into a playlist (playlistItems.insert), ``youtube_remove_from_playlist``
takes one back out (playlistItems.delete), ``youtube_create_playlist`` opens
an empty playlist (playlists.insert) and ``youtube_update_video`` rewrites
a video's snippet metadata (videos.update)."""
import json
import urllib.parse

from src.dapier.connections import tokens
from src.dapier.engine.actions import base
from src.dapier.engine.actions.templating import render

SEARCH_URL = "https://www.googleapis.com/youtube/v3/search"
PLAYLIST_ITEMS_URL = "https://www.googleapis.com/youtube/v3/playlistItems"
PLAYLISTS_URL = "https://www.googleapis.com/youtube/v3/playlists"
VIDEOS_URL = "https://www.googleapis.com/youtube/v3/videos"
UPLOAD_URL = "https://www.googleapis.com/upload/youtube/v3/videos"
MAX_RESULTS = 5
PLAYLIST_MAX_RESULTS = 25
DOWNLOAD_TIMEOUT = 30
# A video upload is the slowest call a step makes: minutes for a large
# source over a slow link, so a generous timeout instead of the 15s reads.
UPLOAD_TIMEOUT = 300

PRIVACY_STATUSES = ("public", "unlisted", "private")

# The media part's type. Dapier does not transcode, so the exact subtype is
# the uploader's concern; every YouTube-accepted source is a video/* mime.
MEDIA_CONTENT_TYPE = "video/mp4"

# Multipart delimiter: the metadata and media parts share it (sent in the
# request's content-type header). Long and prefixed so uploaded bytes never
# contain it by accident.
UPLOAD_BOUNDARY = "dapier-youtube-multipart-9b2e47f"

# The upload stages the whole video in memory (the Lambda has no scratch
# disk to spill to), so sources should keep videos modest; anything larger
# fails with a clear error instead of OOMing the run.
MAX_VIDEO_BYTES = 100 * 1024 * 1024


def _youtube_connection(connection_id):
    return base._connected_connection(connection_id)


def _search_videos(access_token, query, *, transport=None):
    """One ``search.list`` call; returns the items list or raises RuntimeError."""
    transport = transport or base._default_transport
    url = SEARCH_URL + "?" + urllib.parse.urlencode({
        "part": "snippet",
        "q": query,
        "type": "video",
        "maxResults": MAX_RESULTS,
    })
    headers = {
        "authorization": f"Bearer {access_token}",
    }
    try:
        status, response = transport("GET", url, headers=headers, body=None, timeout=15)
    except Exception as exc:
        raise RuntimeError(f"youtube search unreachable: {type(exc).__name__}")
    if status >= 300:
        detail = ""
        try:
            error = json.loads(response.decode() or "{}").get("error")
            if isinstance(error, dict) and error.get("message"):
                detail = f" ({str(error['message'])[:200]})"
        except (ValueError, UnicodeDecodeError):
            pass
        raise RuntimeError(f"youtube search returned HTTP {status}{detail}")
    try:
        result = json.loads(response.decode() or "{}")
    except (ValueError, UnicodeDecodeError):
        return []
    items = result.get("items")
    return items if isinstance(items, list) else []


def run_youtube_find_video(action, event, *, transport=None, steps=None):
    """Find the top videos for a query via the YouTube Data API search.list.

    Zero results are a verdict (``found: False``), not an error, so a
    workflow can branch on whether the search matched anything. The first
    hit is mirrored at ``video`` so the common "link the top result" chain
    does not need a ``videos[0]`` step.
    """
    connection = _youtube_connection(action["connection_id"])
    access_token, _info = tokens.get_access_token(connection, transport=transport)
    query = render(str(action.get("query") or ""), event, steps).strip()
    if not query:
        raise ValueError("youtube_find_video requires a query")
    items = _search_videos(access_token, query, transport=transport)
    videos = []
    for item in items:
        if not isinstance(item, dict):
            continue
        id_block = item.get("id")
        video_id = id_block.get("videoId") if isinstance(id_block, dict) else None
        if not video_id:
            continue
        snippet = item.get("snippet") or {}
        videos.append({
            "id": video_id,
            "title": snippet.get("title"),
            "channel": snippet.get("channelTitle"),
            "published": snippet.get("publishedAt"),
        })
    return {
        "found": bool(videos),
        "count": len(videos),
        "videos": videos,
        "video": videos[0] if videos else None,
    }


def _playlist_page(access_token, playlist_id, *, transport=None):
    """One ``playlistItems.list`` call; returns ``(status, items, detail)``
    without raising on the HTTP status so a dead playlist id can stay a
    verdict the same way a missing meeting does."""
    transport = transport or base._default_transport
    url = PLAYLIST_ITEMS_URL + "?" + urllib.parse.urlencode({
        "part": "snippet,contentDetails",
        "playlistId": playlist_id,
        "maxResults": PLAYLIST_MAX_RESULTS,
    })
    headers = {"authorization": f"Bearer {access_token}"}
    try:
        status, response = transport("GET", url, headers=headers, body=None, timeout=15)
    except Exception as exc:
        raise RuntimeError(f"youtube playlist unreachable: {type(exc).__name__}")
    try:
        result = json.loads(response.decode() or "{}")
    except (ValueError, UnicodeDecodeError):
        result = {}
    if status >= 300:
        error = result.get("error") if isinstance(result, dict) else None
        detail = str(error.get("message") or "")[:200] if isinstance(error, dict) else ""
        return status, [], detail
    items = result.get("items") if isinstance(result, dict) else None
    return status, items if isinstance(items, list) else [], ""


def run_youtube_find_playlist_items(action, event, *, transport=None, steps=None):
    """List a playlist's videos (playlistItems.list), newest first.

    Same output shape as ``youtube_find_video`` — ``{found, count, videos,
    video}`` with the first entry mirrored at ``video`` — so a for_each
    over ``videos`` or a "link the newest" chain read identically. A dead
    playlist id (YouTube 404) is a verdict, not an error.
    """
    connection = _youtube_connection(action["connection_id"])
    access_token, _info = tokens.get_access_token(connection, transport=transport)
    playlist_id = render(str(action.get("playlist_id") or ""), event, steps).strip()
    if not playlist_id:
        raise ValueError("youtube_find_playlist_items requires a playlist_id")
    status, items, detail = _playlist_page(access_token, playlist_id, transport=transport)
    if status == 404:
        return {"found": False, "count": 0, "videos": [], "video": None}
    if status >= 300:
        raise RuntimeError(
            f"youtube playlist returned HTTP {status}{': ' + detail if detail else ''}")
    videos = []
    for item in items:
        if not isinstance(item, dict):
            continue
        snippet = item.get("snippet") or {}
        details = item.get("contentDetails") or {}
        video_id = details.get("videoId") or (snippet.get("resourceId") or {}).get("videoId")
        if not video_id:
            continue
        videos.append({
            "id": video_id,
            "title": snippet.get("title"),
            "channel": snippet.get("channelTitle"),
            "published": snippet.get("publishedAt"),
        })
    return {
        "found": bool(videos),
        "count": len(videos),
        "videos": videos,
        "video": videos[0] if videos else None,
    }


# --- youtube_upload_video: one video onto the channel, sourced like s3_upload -


def _video_content(action, event, steps, *, transport=None):
    """The uploaded bytes: an HTTP download, a staged S3 object, or inline
    text — exactly one, mirroring s3_upload's source composition (and
    drive_upload_file's).

    ``source_s3`` bucket/key take templates, so an earlier step's staged
    file (dropbox_read_file's output, say) uploads without a literal
    bucket/key in the workflow; inline ``content`` takes templates too.
    """
    transport = transport or base._default_transport
    url = render(str(action.get("source_url") or ""), event, steps).strip()
    inline = render(str(action.get("content") or ""), event, steps)
    source = action.get("source_s3") if isinstance(action.get("source_s3"), dict) else {}
    staged = {
        "bucket": render(str(source.get("bucket") or ""), event, steps).strip(),
        "key": render(str(source.get("key") or ""), event, steps).strip(),
    }
    given = [name for name, value in (
        ("source_url", url),
        ("source_s3", staged["bucket"] and staged["key"]),
        ("content", inline),
    ) if value]
    if len(given) > 1:
        raise ValueError("youtube_upload_video takes one content source "
                         f"({', '.join(given)} given): source_url, source_s3, or content")
    if url:
        try:
            status, body = transport("GET", url, headers={}, body=None,
                                     timeout=DOWNLOAD_TIMEOUT)
        except Exception as exc:
            raise RuntimeError(f"video download unreachable: {type(exc).__name__}") from None
        if status >= 300:
            raise RuntimeError(f"video download returned HTTP {status}")
        return body
    if staged["bucket"] and staged["key"]:
        return base._s3_body(staged)
    if inline:
        return inline.encode()
    raise ValueError("youtube_upload_video needs source_url, source_s3 "
                     "with bucket and key, or content")


def _multipart_body(metadata, content, content_type, boundary):
    """One multipart/related body: the metadata JSON part, then the media."""
    head = (
        f"--{boundary}\r\n"
        "Content-Type: application/json; charset=UTF-8\r\n\r\n"
        f"{json.dumps(metadata)}\r\n"
        f"--{boundary}\r\n"
        f"Content-Type: {content_type}\r\n\r\n"
    ).encode()
    return head + content + f"\r\n--{boundary}--\r\n".encode()


def _response_detail(response):
    """The provider's error message out of a JSON error body (truncated)."""
    try:
        error = json.loads(response.decode() or "{}").get("error")
        if isinstance(error, dict) and error.get("message"):
            return f" ({str(error['message'])[:200]})"
    except (ValueError, UnicodeDecodeError, AttributeError):
        pass
    return ""


def _video_metadata(action, event, steps, title, privacy_status):
    """The videos.insert document: ``snippet`` carries the presentation
    fields (only the ones given — YouTube validates categoryId), and
    ``status.privacyStatus`` the visibility (unlisted by default, like
    Zapier's Upload Video). ``tags`` is a comma-separated string that
    becomes the list the API wants; every field takes templates."""
    snippet = {"title": title}
    description = render(str(action.get("description") or ""), event, steps).strip()
    if description:
        snippet["description"] = description
    tags = [tag.strip() for tag in render(str(action.get("tags") or ""), event, steps).split(",")
            if tag.strip()]
    if tags:
        snippet["tags"] = tags
    category_id = render(str(action.get("category_id") or ""), event, steps).strip()
    if category_id:
        snippet["categoryId"] = category_id
    return {"snippet": snippet, "status": {"privacyStatus": privacy_status}}


def run_youtube_upload_video(action, event, *, transport=None, steps=None):
    """Upload one video to the connection's channel (Data API multipart
    videos.insert).

    The bytes come from exactly one content source — ``source_url`` (an
    HTTP download), ``source_s3`` ``{bucket, key}`` (a staged object), or
    inline ``content`` — and stage in memory, so ``MAX_VIDEO_BYTES`` caps
    them (a bigger source is a clear ``ValueError``, not an OOM). The
    connection's OAuth grant must include the ``youtube.upload`` scope; a
    read-only token fails with the API's HTTP 403. The output carries the
    created video's facts: ``video_id``, ``title``, ``privacy_status``,
    ``upload_status``, ``url``.
    """
    connection = _youtube_connection(action["connection_id"])
    transport = transport or base._default_transport
    access_token, _info = tokens.get_access_token(connection, transport=transport)
    title = render(str(action.get("title") or ""), event, steps).strip()
    if not title:
        raise ValueError("youtube_upload_video requires a title")
    privacy_status = str(action.get("privacy_status") or "unlisted").strip().lower()
    if privacy_status not in PRIVACY_STATUSES:
        raise ValueError(
            "youtube_upload_video privacy_status must be one of: "
            f"{', '.join(PRIVACY_STATUSES)}")
    content = _video_content(action, event, steps, transport=transport)
    if len(content) > MAX_VIDEO_BYTES:
        raise ValueError(
            "youtube_upload_video stages the video in memory, capped at "
            f"{MAX_VIDEO_BYTES} bytes (got {len(content)}): keep the source modest")
    metadata = _video_metadata(action, event, steps, title, privacy_status)
    url = UPLOAD_URL + "?" + urllib.parse.urlencode({
        "uploadType": "multipart",
        "part": "snippet,status",
    })
    headers = {
        "authorization": f"Bearer {access_token}",
        "content-type": f"multipart/related; boundary={UPLOAD_BOUNDARY}",
    }
    body = _multipart_body(metadata, content, MEDIA_CONTENT_TYPE, UPLOAD_BOUNDARY)
    try:
        status, response = transport("POST", url, headers=headers, body=body,
                                     timeout=UPLOAD_TIMEOUT)
    except Exception as exc:
        raise RuntimeError(f"youtube upload unreachable: {type(exc).__name__}") from None
    if status >= 300:
        raise RuntimeError(
            f"youtube upload returned HTTP {status}{_response_detail(response)}")
    try:
        result = json.loads(response.decode() or "{}")
    except (ValueError, UnicodeDecodeError):
        result = {}
    video = result if isinstance(result, dict) else {}
    snippet = video.get("snippet") if isinstance(video.get("snippet"), dict) else {}
    video_status = video.get("status") if isinstance(video.get("status"), dict) else {}
    video_id = video.get("id")
    return {
        "video_id": video_id,
        "title": snippet.get("title"),
        "privacy_status": video_status.get("privacyStatus"),
        "upload_status": video_status.get("uploadStatus"),
        "url": f"https://www.youtube.com/watch?v={video_id}" if video_id else None,
    }


# --- youtube_add_to_playlist / youtube_update_video: the write-back staples ----


def run_youtube_add_to_playlist(action, event, *, transport=None, steps=None):
    """Add one video to a playlist the connection can edit (Data API
    playlistItems.insert — Zapier's Add Video to Playlist).

    ``playlist_id`` and ``video_id`` take templates, so a youtube trigger's
    ``{trigger.video_id}`` or a found playlist's id chains straight in.
    Adding a video that is already in the playlist is YouTube's
    ``videoAlreadyInPlaylist`` error (HTTP 409-style), not a silent
    duplicate. Output: ``{playlist_id, video_id, playlist_item_id, title,
    position}`` — the new listing as YouTube stores it.
    """
    connection = _youtube_connection(action["connection_id"])
    transport = transport or base._default_transport
    access_token, _info = tokens.get_access_token(connection, transport=transport)
    playlist_id = render(str(action.get("playlist_id") or ""), event, steps).strip()
    if not playlist_id:
        raise ValueError("youtube_add_to_playlist requires a playlist_id")
    video_id = render(str(action.get("video_id") or ""), event, steps).strip()
    if not video_id:
        raise ValueError("youtube_add_to_playlist requires a video_id")
    body = {
        "snippet": {
            "playlistId": playlist_id,
            "resourceId": {"kind": "youtube#video", "videoId": video_id},
        },
    }
    url = PLAYLIST_ITEMS_URL + "?" + urllib.parse.urlencode({"part": "snippet"})
    headers = {
        "authorization": f"Bearer {access_token}",
        "content-type": "application/json",
    }
    try:
        status, response = transport("POST", url, headers=headers,
                                     body=json.dumps(body, separators=(",", ":")).encode(),
                                     timeout=15)
    except Exception as exc:
        raise RuntimeError(
            f"youtube playlist unreachable: {type(exc).__name__}") from None
    if status >= 300:
        raise RuntimeError("youtube add-to-playlist returned HTTP "
                           f"{status}{_response_detail(response)}")
    try:
        result = json.loads(response.decode() or "{}")
    except (ValueError, UnicodeDecodeError):
        result = {}
    item = result if isinstance(result, dict) else {}
    snippet = item.get("snippet") if isinstance(item.get("snippet"), dict) else {}
    resource = snippet.get("resourceId") if isinstance(snippet.get("resourceId"), dict) else {}
    return {
        "playlist_id": snippet.get("playlistId") or playlist_id,
        "video_id": resource.get("videoId") or video_id,
        "playlist_item_id": item.get("id"),
        "title": snippet.get("title"),
        "position": snippet.get("position"),
    }


def run_youtube_remove_from_playlist(action, event, *, transport=None, steps=None):
    """Remove one item from a playlist the connection can edit (Data API
    playlistItems.delete — Zapier's Remove Video from Playlist).

    ``playlist_item_id`` is the playlist item's own id — the one
    :func:`run_youtube_add_to_playlist` returns as ``playlist_item_id``, or
    an item id from a :func:`run_youtube_find_playlist_items` listing — not
    the video id. An item already gone (YouTube 404) is a verdict:
    ``{removed: False}``, not an error, so a cleanup chain can re-run.
    Output: ``{removed, playlist_item_id}``.
    """
    connection = _youtube_connection(action["connection_id"])
    transport = transport or base._default_transport
    access_token, _info = tokens.get_access_token(connection, transport=transport)
    playlist_item_id = render(str(action.get("playlist_item_id") or ""),
                              event, steps).strip()
    if not playlist_item_id:
        raise ValueError("youtube_remove_from_playlist requires a playlist_item_id")
    url = PLAYLIST_ITEMS_URL + "?" + urllib.parse.urlencode(
        {"id": playlist_item_id})
    headers = {"authorization": f"Bearer {access_token}"}
    try:
        status, response = transport("DELETE", url, headers=headers,
                                     body=None, timeout=15)
    except Exception as exc:
        raise RuntimeError(
            f"youtube playlist unreachable: {type(exc).__name__}") from None
    if status == 404:
        # Already removed (or never there): a benign miss, like
        # mailchimp_remove_member's missing member.
        return {"removed": False, "playlist_item_id": playlist_item_id}
    if status >= 300:
        raise RuntimeError("youtube remove-from-playlist returned HTTP "
                           f"{status}{_response_detail(response)}")
    return {"removed": True, "playlist_item_id": playlist_item_id}


def run_youtube_create_playlist(action, event, *, transport=None, steps=None):
    """Create an empty playlist on the connection's channel (Data API
    playlists.insert, ``part=snippet,status`` — Zapier's Create Playlist).

    ``title`` is required; ``description`` and ``privacy_status``
    (private|public|unlisted, private by default like Zapier's Create
    Playlist) ride along when given. Output: ``{playlist_id, title, url}``
    shaped like the other youtube runners — the id chains straight into
    :func:`run_youtube_add_to_playlist`.
    """
    connection = _youtube_connection(action["connection_id"])
    transport = transport or base._default_transport
    access_token, _info = tokens.get_access_token(connection, transport=transport)
    title = render(str(action.get("title") or ""), event, steps).strip()
    if not title:
        raise ValueError("youtube_create_playlist requires a title")
    privacy_status = str(action.get("privacy_status") or "private").strip().lower()
    if privacy_status not in PRIVACY_STATUSES:
        raise ValueError(
            "youtube_create_playlist privacy_status must be one of: "
            f"{', '.join(PRIVACY_STATUSES)}")
    snippet = {"title": title}
    description = render(str(action.get("description") or ""), event, steps).strip()
    if description:
        snippet["description"] = description
    body = {"snippet": snippet, "status": {"privacyStatus": privacy_status}}
    url = PLAYLISTS_URL + "?" + urllib.parse.urlencode(
        {"part": "snippet,status"})
    headers = {
        "authorization": f"Bearer {access_token}",
        "content-type": "application/json",
    }
    try:
        status, response = transport("POST", url, headers=headers,
                                     body=json.dumps(body, separators=(",", ":")).encode(),
                                     timeout=15)
    except Exception as exc:
        raise RuntimeError(
            f"youtube playlists unreachable: {type(exc).__name__}") from None
    if status >= 300:
        raise RuntimeError(
            f"youtube create-playlist returned HTTP "
            f"{status}{_response_detail(response)}")
    try:
        result = json.loads(response.decode() or "{}")
    except (ValueError, UnicodeDecodeError):
        result = {}
    playlist = result if isinstance(result, dict) else {}
    created_snippet = (playlist.get("snippet")
                       if isinstance(playlist.get("snippet"), dict) else {})
    playlist_id = playlist.get("id")
    return {
        "playlist_id": playlist_id,
        "title": created_snippet.get("title") or title,
        "url": (f"https://www.youtube.com/playlist?list={playlist_id}"
                if playlist_id else None),
    }


def run_youtube_update_video(action, event, *, transport=None, steps=None):
    """Update one video's snippet metadata (Data API videos.update,
    ``part=snippet`` — Zapier's Update Video).

    YouTube replaces the whole snippet part on update, so ``title`` is
    required (the API rejects a snippet without one) and ``description`` /
    ``category_id`` ride along only when given — pass ``category_id`` when
    the video's category matters, or the update clears it. Output:
    ``{updated: true, video_id, title, description, category_id}``.
    """
    connection = _youtube_connection(action["connection_id"])
    transport = transport or base._default_transport
    access_token, _info = tokens.get_access_token(connection, transport=transport)
    video_id = render(str(action.get("video_id") or ""), event, steps).strip()
    if not video_id:
        raise ValueError("youtube_update_video requires a video_id")
    title = render(str(action.get("title") or ""), event, steps).strip()
    if not title:
        raise ValueError("youtube_update_video requires a title (the snippet "
                         "part is replaced whole)")
    snippet = {"title": title}
    description = render(str(action.get("description") or ""), event, steps)
    if description.strip():
        snippet["description"] = description
    category_id = render(str(action.get("category_id") or ""), event, steps).strip()
    if category_id:
        snippet["categoryId"] = category_id
    body = {"id": video_id, "snippet": snippet}
    url = VIDEOS_URL + "?" + urllib.parse.urlencode({"part": "snippet"})
    headers = {
        "authorization": f"Bearer {access_token}",
        "content-type": "application/json",
    }
    try:
        status, response = transport("PUT", url, headers=headers,
                                     body=json.dumps(body, separators=(",", ":")).encode(),
                                     timeout=15)
    except Exception as exc:
        raise RuntimeError(f"youtube update unreachable: {type(exc).__name__}") from None
    if status >= 300:
        raise RuntimeError(
            f"youtube update returned HTTP {status}{_response_detail(response)}")
    try:
        result = json.loads(response.decode() or "{}")
    except (ValueError, UnicodeDecodeError):
        result = {}
    video = result if isinstance(result, dict) else {}
    updated_snippet = (video.get("snippet")
                       if isinstance(video.get("snippet"), dict) else snippet)
    return {
        "updated": True,
        "video_id": video.get("id") or video_id,
        "title": updated_snippet.get("title") or title,
        "description": updated_snippet.get("description"),
        "category_id": updated_snippet.get("categoryId") or category_id or None,
    }


def run_youtube_subscription_status(action, event, *, steps=None):
    from src.dapier.triggers.intake.youtube_subscriptions import subscription_status

    channel_id = render(str(action.get("channel_id") or ""), event, steps).strip()
    return subscription_status(channel_id)


def run_youtube_subscription_renew(action, event, *, steps=None):
    from src.dapier.triggers.intake.youtube_subscriptions import renew_subscription
    channel_id = render(str(action.get("channel_id") or ""), event, steps).strip()
    return renew_subscription(channel_id)
