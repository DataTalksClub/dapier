"""youtube_find_video / youtube_find_playlist_items: search YouTube and list
playlist videos through a Google connection."""
import json
import urllib.parse

from ...connections import tokens
from . import base
from .templating import render

SEARCH_URL = "https://www.googleapis.com/youtube/v3/search"
PLAYLIST_ITEMS_URL = "https://www.googleapis.com/youtube/v3/playlistItems"
MAX_RESULTS = 5
PLAYLIST_MAX_RESULTS = 25


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
