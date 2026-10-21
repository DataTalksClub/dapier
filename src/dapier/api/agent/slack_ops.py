"""Slack DM/export helpers for the agent API (connection token stays server-side)."""

from ...connections import discovery as slack_discovery
from ...connections import tokens
from ...connections.providers import oauth_providers
from .. import discovery as discovery_api

DEFAULT_CONNECTION_ID = "slack-alexey"


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


def dm_export(user, *, connection_id=DEFAULT_CONNECTION_ID, limit=200, cursor=None):
    """find/open DM + history in one call."""
    opened = open_dm(user, connection_id=connection_id)
    hist = history(
        opened["channel"], connection_id=connection_id, limit=limit, cursor=cursor)
    return {**opened, **hist, "user_id": opened["user_id"]}
