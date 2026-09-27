"""youtube_find_video action: search YouTube through a Google connection."""
import json
import urllib.parse

from ...connections import tokens
from . import base
from .templating import render

SEARCH_URL = "https://www.googleapis.com/youtube/v3/search"
MAX_RESULTS = 5


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
