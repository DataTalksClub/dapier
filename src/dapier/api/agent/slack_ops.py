"""Slack DM/export/post helpers for the agent API (connection token stays server-side)."""

import re
from urllib.parse import urlencode

from ...connections import discovery as slack_discovery
from ...connections import tokens
from ...connections.providers import oauth_providers
from .. import discovery as discovery_api

DEFAULT_CONNECTION_ID = "slack-alexey"
DEFAULT_POST_CONNECTION_ID = "slack"
_CHANNEL_ID_RE = re.compile(r"^[CGD][A-Z0-9]+$")


class SlackOpsError(Exception):
    """Operator-facing Slack failure; message must never carry a token."""

    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


def _connection_and_token(connection_id):
    connection_id = str(connection_id or DEFAULT_CONNECTION_ID).strip().lower()
    connection, error = discovery_api._load(connection_id, None)
    if error:
        status, payload = error
        raise SlackOpsError(payload.get("error") or "Connection not found", status=status)
    if str(connection.get("provider") or "").lower() != "slack":
        raise SlackOpsError(f"Connection {connection_id} is not a Slack connection", status=400)
    try:
        token, _info = tokens.get_access_token(connection)
    except Exception as exc:
        raise SlackOpsError(
            f"Slack token unavailable for {connection_id}: {type(exc).__name__}",
            status=502,
        ) from None
    return connection_id, connection, token


def _call(token, method, payload):
    try:
        return slack_discovery._slack_call(method, token, payload)
    except slack_discovery.DiscoveryError as exc:
        raise SlackOpsError(str(exc), status=getattr(exc, "status", 502) or 502) from None


def find_users(query, *, connection_id=DEFAULT_CONNECTION_ID, limit=200):
    """Match workspace users by id, handle, name, or email (case-insensitive)."""
    connection_id, connection, token = _connection_and_token(connection_id)
    q = str(query or "").strip().lower()
    if not q:
        raise SlackOpsError("query is required")
    # Pull a generous page via existing discovery helper.
    items = slack_discovery._slack_users(connection, token, {}, max(limit, 200))
    matches = []
    for item in items:
        hay = " ".join(
            str(item.get(key) or "")
            for key in ("id", "name", "handle", "email")
        ).lower()
        if q in hay or q == str(item.get("id") or "").lower():
            matches.append(item)
        if len(matches) >= limit:
            break
    return {"connection": connection_id, "query": query, "items": matches, "count": len(matches)}


def open_dm(user, *, connection_id=DEFAULT_CONNECTION_ID):
    """Resolve a user (id or find-user query) and open/return their IM channel."""
    connection_id, connection, token = _connection_and_token(connection_id)
    user = str(user or "").strip()
    if not user:
        raise SlackOpsError("user is required")
    user_id = user
    if not user.startswith("U") and not user.startswith("W"):
        found = find_users(user, connection_id=connection_id, limit=5)["items"]
        if not found:
            raise SlackOpsError(f"No Slack user matched {user!r}", status=404)
        if len(found) > 1:
            labels = ", ".join(
                f"{u.get('id')} ({u.get('name') or u.get('handle')})" for u in found[:8]
            )
            raise SlackOpsError(
                f"Ambiguous user {user!r}; matches: {labels}. Pass a user id.",
                status=400,
            )
        user_id = found[0]["id"]
    data = _call(token, "conversations.open", {"users": user_id})
    channel = (data.get("channel") or {})
    channel_id = channel.get("id")
    if not channel_id:
        raise SlackOpsError("Slack conversations.open returned no channel", status=502)
    return {
        "connection": connection_id,
        "user_id": user_id,
        "channel": channel_id,
        "channel_type": "im",
    }


def history(channel, *, connection_id=DEFAULT_CONNECTION_ID, limit=100, cursor=None,
            oldest=None, latest=None):
    """Dump conversations.history for a channel/DM (paginated)."""
    connection_id, _connection, token = _connection_and_token(connection_id)
    channel = str(channel or "").strip()
    if not channel:
        raise SlackOpsError("channel is required")
    limit = max(1, min(int(limit or 100), 1000))
    page_size = min(limit, 200)
    payload = {"channel": channel, "limit": page_size}
    if cursor:
        payload["cursor"] = str(cursor)
    if oldest:
        payload["oldest"] = str(oldest)
    if latest:
        payload["latest"] = str(latest)
    messages = []
    next_cursor = None
    while len(messages) < limit:
        payload["limit"] = min(page_size, limit - len(messages))
        data = _call(token, "conversations.history", payload)
        batch = [
            {
                "ts": m.get("ts"),
                "user": m.get("user"),
                "bot_id": m.get("bot_id"),
                "type": m.get("type"),
                "subtype": m.get("subtype"),
                "text": m.get("text") or "",
                "thread_ts": m.get("thread_ts"),
                "reply_count": m.get("reply_count"),
                "latest_reply": m.get("latest_reply"),
            }
            for m in (data.get("messages") or [])
            if isinstance(m, dict) and m.get("ts")
        ]
        messages.extend(batch)
        next_cursor = (data.get("response_metadata") or {}).get("next_cursor") or None
        if not next_cursor or not batch:
            break
        payload = {**payload, "cursor": next_cursor}
    # Slack returns newest-first; keep that order for export fidelity.
    return {
        "connection": connection_id,
        "channel": channel,
        "messages": messages[:limit],
        "count": len(messages[:limit]),
        "next_cursor": next_cursor,
    }


def _message(m):
    return {
        "ts": m.get("ts"),
        "user": m.get("user"),
        "bot_id": m.get("bot_id"),
        "subtype": m.get("subtype"),
        "text": m.get("text") or "",
        "thread_ts": m.get("thread_ts"),
        "reply_count": m.get("reply_count"),
    }


def replies(channel, thread_ts, *, connection_id=DEFAULT_CONNECTION_ID, limit=200,
            cursor=None):
    """conversations.replies for one thread (root message first), paginated."""
    connection_id, _connection, token = _connection_and_token(connection_id)
    channel = str(channel or "").strip()
    thread_ts = str(thread_ts or "").strip()
    if not channel or not thread_ts:
        raise SlackOpsError("channel and thread_ts are required")
    limit = max(1, min(int(limit or 200), 1000))
    payload = {"channel": channel, "ts": thread_ts, "limit": min(limit, 200)}
    if cursor:
        payload["cursor"] = str(cursor)
    messages = []
    next_cursor = None
    while len(messages) < limit:
        data = _call(token, "conversations.replies", payload)
        messages.extend(_message(m) for m in (data.get("messages") or [])
                        if isinstance(m, dict) and m.get("ts"))
        next_cursor = (data.get("response_metadata") or {}).get("next_cursor") or None
        if not next_cursor:
            break
        payload = {**payload, "cursor": next_cursor}
    return {
        "connection": connection_id,
        "channel": channel,
        "thread_ts": thread_ts,
        "messages": messages[:limit],
        "count": len(messages[:limit]),
        "next_cursor": next_cursor,
    }


def channels(*, connection_id=DEFAULT_CONNECTION_ID, limit=200, cursor=None,
             types="public_channel,private_channel"):
    """conversations.list with a cursor so callers can page past 25."""
    connection_id, _connection, token = _connection_and_token(connection_id)
    limit = max(1, min(int(limit or 200), 1000))
    payload = {"limit": min(limit, 200), "exclude_archived": True, "types": str(types)}
    if cursor:
        payload["cursor"] = str(cursor)
    items = []
    next_cursor = None
    while len(items) < limit:
        data = _call(token, "conversations.list", payload)
        items.extend(
            {"id": c.get("id"), "name": c.get("name"),
             "type": "private" if c.get("is_private") else "channel",
             "num_members": c.get("num_members")}
            for c in (data.get("channels") or []) if isinstance(c, dict) and c.get("id"))
        next_cursor = (data.get("response_metadata") or {}).get("next_cursor") or None
        if not next_cursor:
            break
        payload = {**payload, "cursor": next_cursor}
    return {"connection": connection_id, "items": items[:limit],
            "count": len(items[:limit]), "next_cursor": next_cursor}


def search(query, *, connection_id=DEFAULT_CONNECTION_ID, count=20, page=1):
    """search.messages (needs the search:read user scope; bot tokens cannot)."""
    connection_id, _connection, token = _connection_and_token(connection_id)
    query = str(query or "").strip()
    if not query:
        raise SlackOpsError("query is required")
    data = _call(token, "search.messages", {
        "query": query, "count": max(1, min(int(count or 20), 100)),
        "page": max(1, int(page or 1)), "sort": "timestamp", "sort_dir": "asc"})
    block = data.get("messages") or {}
    items = [
        {"ts": m.get("ts"), "channel_id": (m.get("channel") or {}).get("id"),
         "channel_name": (m.get("channel") or {}).get("name"),
         "user": m.get("user") or m.get("username"), "text": m.get("text") or "",
         "permalink": m.get("permalink")}
        for m in block.get("matches") or [] if isinstance(m, dict)]
    paging = block.get("paging") or {}
    return {"connection": connection_id, "query": query, "items": items,
            "count": len(items), "page": paging.get("page"), "pages": paging.get("pages"),
            "total": paging.get("total")}


def dm_export(user, *, connection_id=DEFAULT_CONNECTION_ID, limit=200, cursor=None):
    """find/open DM + history in one call."""
    opened = open_dm(user, connection_id=connection_id)
    hist = history(
        opened["channel"], connection_id=connection_id, limit=limit, cursor=cursor)
    return {**opened, **hist, "user_id": opened["user_id"]}


def _resolve_channel_id(token, channel):
    """Return a Slack channel ID, resolving #name via conversations.list when needed."""
    raw = str(channel or "").strip()
    if not raw:
        raise SlackOpsError("channel is required")
    if _CHANNEL_ID_RE.fullmatch(raw):
        return raw
    target = raw.lstrip("#").strip().lower()
    if not target:
        raise SlackOpsError("channel is required")
    cursor = None
    for _ in range(5):
        payload = {"limit": 200, "exclude_archived": True, "types": "public_channel,private_channel"}
        if cursor:
            payload["cursor"] = cursor
        data = _call(token, "conversations.list", payload)
        for item in data.get("channels") or []:
            if isinstance(item, dict) and str(item.get("name") or "").lower() == target:
                cid = item.get("id")
                if cid:
                    return cid
        cursor = (data.get("response_metadata") or {}).get("next_cursor") or None
        if not cursor:
            break
    raise SlackOpsError(f"Slack channel {channel!r} not found", status=404)


def post_message(channel, text, *, connection_id=DEFAULT_POST_CONNECTION_ID, thread_ts=None,
                 unfurl_links=True, unfurl_media=True, dry_run=False):
    """Post a message (or dry-run check) to a Slack channel/thread via chat.postMessage."""
    connection_id, _connection, token = _connection_and_token(connection_id)
    text = str(text or "")
    if not text.strip():
        raise SlackOpsError("text is required")
    channel_id = _resolve_channel_id(token, channel)
    thread_ts = str(thread_ts).strip() if thread_ts else None
    if dry_run:
        try:
            info = slack_discovery._request(
                "GET",
                f"{slack_discovery.SLACK_API_URL}/conversations.info?{urlencode({'channel': channel_id})}",
                token,
                None,
            )
        except slack_discovery.DiscoveryError as exc:
            raise SlackOpsError(str(exc), status=getattr(exc, "status", 502) or 502) from None
        if not info.get("ok"):
            err = info.get("error") or "unknown_error"
            raise SlackOpsError(f"Slack rejected conversations.info: {err}", status=502)
        ch = info.get("channel") or {}
        auth = _call(token, "auth.test", {})
        return {
            "ok": True,
            "dry_run": True,
            "connection": connection_id,
            "channel": channel_id,
            "channel_name": ch.get("name"),
            "is_member": ch.get("is_member"),
            "bot_id": auth.get("bot_id"),
            "user_id": auth.get("user_id"),
            "thread_ts": thread_ts,
            "unfurl_links": bool(unfurl_links),
            "unfurl_media": bool(unfurl_media),
            "text_length": len(text),
        }
    payload = {
        "channel": channel_id,
        "text": text,
        "unfurl_links": bool(unfurl_links),
        "unfurl_media": bool(unfurl_media),
    }
    if thread_ts:
        payload["thread_ts"] = thread_ts
    data = _call(token, "chat.postMessage", payload)
    return {
        "ok": True,
        "connection": connection_id,
        "channel": data.get("channel") or channel_id,
        "ts": data.get("ts"),
        "thread_ts": thread_ts,
    }
