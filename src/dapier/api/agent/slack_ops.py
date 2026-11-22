"""Slack DM/export/post helpers for the agent API (connection token stays server-side)."""

import json
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
        scopes = _token_scopes(token)
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
            "scopes": scopes,
            "files_write": "files:write" in scopes,
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


# --- Images: files.uploadV2 (getUploadURLExternal → bytes → completeUploadExternal) ---
#
# The bytes never pass through the API: ``image_upload_url`` mints a Slack
# presigned upload URL (the URL itself is the credential, it carries no token),
# the CLI POSTs the file there, and ``post_image``/``attach_image`` publish the
# uploaded file id with ``files.completeUploadExternal``. Slack cannot add a
# file to an existing message, so ``attach_image`` either posts the image as a
# thread reply, or (``replace``) reposts text+image and deletes the old message.

SLACK_MAX_FILE_BYTES = 1024 * 1024 * 1024  # Slack's per-file limit (1 GB)
IMAGE_EXTENSIONS = frozenset({
    "png", "jpg", "jpeg", "gif", "webp", "bmp", "tif", "tiff", "heic", "svg",
})
_FILE_ID_RE = re.compile(r"^F[A-Z0-9]+$")
_TS_RE = re.compile(r"^\d+\.\d+$")
# Slack shares a completed upload asynchronously; poll this long for its ts.
SHARE_LOOKUP_DELAYS = (0.5, 1, 1, 2, 2, 2)

_SLACK_ERROR_HINTS = {
    "missing_scope": "the Slack token lacks a required scope",
    "not_in_channel": "the bot is not a member of this channel; invite the bot to it first",
    "channel_not_found": "channel not found or not visible to the bot",
    "file_too_large": "the file is larger than Slack accepts",
    "invalid_length": "file length is invalid (empty or over Slack's limit)",
    "file_not_found": "the uploaded file id is unknown (upload not finished or expired)",
    "file_uploads_disabled": "file uploads are disabled in this workspace",
    "message_not_found": "no message with that ts in this channel",
    "cant_delete_message": "the bot cannot delete this message (not posted by it)",
    "thread_not_found": "no thread with that ts in this channel",
    "is_archived": "the channel is archived",
    "restricted_action": "workspace policy blocks this action for the bot",
}


def _rpc(token, method, params, *, http_method="POST"):
    """One form-encoded Slack Web API call; raises SlackOpsError with a clear,
    token-free reason (missing scope names the needed scope)."""
    params = {k: v for k, v in params.items() if v is not None}
    url = f"{slack_discovery.SLACK_API_URL}/{method}"
    try:
        if http_method == "GET":
            data = slack_discovery._request(
                "GET", f"{url}?{urlencode(params)}", token, None)
        else:
            data = slack_discovery._request("POST", url, token, params, form=True)
    except slack_discovery.DiscoveryError as exc:
        raise SlackOpsError(str(exc), status=getattr(exc, "status", 502) or 502) from None
    if data.get("ok"):
        return data
    error = oauth_providers.redact(str(data.get("error") or "unknown_error"))
    hint = _SLACK_ERROR_HINTS.get(error)
    message = f"Slack rejected {method}: {error}"
    if hint:
        message += f" ({hint})"
    if error == "missing_scope":
        needed = oauth_providers.redact(str(data.get("needed") or "?"))
        provided = oauth_providers.redact(str(data.get("provided") or "?"))
        message += (f"; needed scope: {needed}; token has: {provided}. "
                    "Add the scope to the Slack app and reinstall it to the workspace.")
        raise SlackOpsError(message, status=403)
    status = 413 if error in ("file_too_large", "invalid_length") else 502
    raise SlackOpsError(message, status=status)


def _validate_image_name(filename):
    name = str(filename or "").strip().replace("\\", "/").rsplit("/", 1)[-1]
    if not name:
        raise SlackOpsError("filename is required")
    ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
    if ext not in IMAGE_EXTENSIONS:
        raise SlackOpsError(
            f"{name!r} is not an image (allowed extensions: {', '.join(sorted(IMAGE_EXTENSIONS))})")
    return name


def image_upload_url(filename, length, *, connection_id=DEFAULT_POST_CONNECTION_ID):
    """Mint a Slack presigned upload URL for one image (files.getUploadURLExternal)."""
    connection_id, _connection, token = _connection_and_token(connection_id)
    name = _validate_image_name(filename)
    try:
        length = int(length)
    except (TypeError, ValueError):
        raise SlackOpsError("length must be an integer byte count") from None
    if length <= 0:
        raise SlackOpsError("image file is empty")
    if length > SLACK_MAX_FILE_BYTES:
        raise SlackOpsError(
            f"image is too big: {length} bytes > Slack limit {SLACK_MAX_FILE_BYTES} bytes",
            status=413)
    data = _rpc(token, "files.getUploadURLExternal",
                {"filename": name, "length": length}, http_method="GET")
    upload_url = str(data.get("upload_url") or "")
    file_id = str(data.get("file_id") or "")
    if not upload_url or not file_id:
        raise SlackOpsError("Slack returned no upload target", status=502)
    return {"connection": connection_id, "upload_url": upload_url, "file_id": file_id,
            "filename": name, "length": length}


def _file_id(value):
    fid = str(value or "").strip()
    if not _FILE_ID_RE.fullmatch(fid):
        raise SlackOpsError("file_id is required (an F… id from the upload-url step)")
    return fid


def _ts(value, field):
    ts = str(value or "").strip()
    if not _TS_RE.fullmatch(ts):
        raise SlackOpsError(f"{field} must be a Slack ts like 1700000000.000100")
    return ts


def _find_share_ts(token, channel_id, file_id, thread_ts, oldest, sleep):
    """The ts of the message that carries ``file_id`` (None if not found in time)."""
    for delay in SHARE_LOOKUP_DELAYS:
        sleep(delay)
        if thread_ts:
            data = _rpc(token, "conversations.replies",
                        {"channel": channel_id, "ts": thread_ts, "oldest": oldest,
                         "limit": 50}, http_method="GET")
        else:
            data = _rpc(token, "conversations.history",
                        {"channel": channel_id, "oldest": oldest, "limit": 20},
                        http_method="GET")
        for message in data.get("messages") or []:
            if not isinstance(message, dict):
                continue
            ids = {f.get("id") for f in message.get("files") or [] if isinstance(f, dict)}
            if file_id in ids:
                return message.get("ts")
    return None


def _complete(token, channel_id, file_id, title, text, thread_ts, sleep):
    """Share an uploaded file into a channel/thread with ``text`` as the
    initial comment; returns (file info, message ts or None, ts lookup error)."""
    import time

    oldest = f"{time.time() - 60:.6f}"
    completion = {
        "files": json.dumps([{"id": file_id, "title": title or file_id}]),
        "channel_id": channel_id,
    }
    if text:
        completion["initial_comment"] = text
    if thread_ts:
        completion["thread_ts"] = thread_ts
    data = _rpc(token, "files.completeUploadExternal", completion)
    files = [f for f in data.get("files") or [] if isinstance(f, dict)]
    first = files[0] if files else {}
    info = {"id": first.get("id") or file_id, "name": first.get("name"),
            "title": first.get("title"), "permalink": first.get("permalink")}
    try:
        ts = _find_share_ts(token, channel_id, file_id, thread_ts, oldest, sleep)
        lookup_error = None if ts else "posted, but the message ts did not appear in time"
    except SlackOpsError as exc:
        ts, lookup_error = None, f"posted, but the message ts lookup failed: {exc}"
    return info, ts, lookup_error


def post_image(channel, text, file_id, *, connection_id=DEFAULT_POST_CONNECTION_ID,
               thread_ts=None, title=None, sleep=None):
    """Post an uploaded image to a channel/thread with ``text`` as its comment."""
    import time

    connection_id, _connection, token = _connection_and_token(connection_id)
    file_id = _file_id(file_id)
    text = str(text or "")
    channel_id = _resolve_channel_id(token, channel)
    thread_ts = _ts(thread_ts, "thread_ts") if thread_ts else None
    info, ts, lookup_error = _complete(token, channel_id, file_id, title, text, thread_ts,
                                       sleep or time.sleep)
    result = {"ok": True, "connection": connection_id, "channel": channel_id, "ts": ts,
              "thread_ts": thread_ts, "file": info}
    if lookup_error:
        result["ts_error"] = lookup_error
    return result


def _fetch_message(token, channel_id, ts):
    """The single message at ``ts`` (root or thread reply)."""
    data = _rpc(token, "conversations.history",
                {"channel": channel_id, "latest": ts, "oldest": ts, "inclusive": "true",
                 "limit": 1}, http_method="GET")
    for message in data.get("messages") or []:
        if isinstance(message, dict) and message.get("ts") == ts:
            return message
    # Thread replies are not in history; conversations.replies accepts a
    # reply's ts and returns its thread (the parent always comes first).
    data = _rpc(token, "conversations.replies",
                {"channel": channel_id, "ts": ts, "oldest": ts, "latest": ts,
                 "inclusive": "true", "limit": 100}, http_method="GET")
    for message in data.get("messages") or []:
        if isinstance(message, dict) and message.get("ts") == ts:
            return message
    raise SlackOpsError(f"Slack message {ts} not found in {channel_id}", status=404)


def attach_image(channel, ts, file_id, *, replace=False, text=None,
                 connection_id=DEFAULT_POST_CONNECTION_ID, title=None, sleep=None):
    """Attach an uploaded image to an existing message.

    Slack cannot add a file to a posted message. Without ``replace`` the image
    is posted as a reply in the message's thread (``text`` is its optional
    comment). With ``replace`` the message — which must be the bot's own and
    have no thread replies — is reposted as text+image at the same place (same
    thread when it was a reply) and the old message is deleted; the new ts is
    returned. The new message is posted before the old one is deleted, so a
    failure never loses the text.
    """
    import time

    sleep = sleep or time.sleep
    connection_id, _connection, token = _connection_and_token(connection_id)
    file_id = _file_id(file_id)
    ts = _ts(ts, "ts")
    channel_id = _resolve_channel_id(token, channel)
    original = _fetch_message(token, channel_id, ts)
    if not replace:
        thread_root = original.get("thread_ts") or ts
        info, new_ts, lookup_error = _complete(
            token, channel_id, file_id, title, str(text or ""), thread_root, sleep)
        result = {"ok": True, "mode": "thread_reply", "connection": connection_id,
                  "channel": channel_id, "target_ts": ts, "thread_ts": thread_root,
                  "ts": new_ts, "file": info}
        if lookup_error:
            result["ts_error"] = lookup_error
        return result

    auth = _call(token, "auth.test", {})
    own = (original.get("bot_id") and original.get("bot_id") == auth.get("bot_id")) or (
        original.get("user") and original.get("user") == auth.get("user_id"))
    if not own:
        raise SlackOpsError(
            f"refusing --replace: message {ts} was not posted by this connection's "
            "bot/user, so it cannot be deleted and reposted", status=403)
    if original.get("files"):
        raise SlackOpsError(
            f"refusing --replace: message {ts} already carries files; "
            "a repost would drop them", status=409)
    is_reply = bool(original.get("thread_ts")) and original.get("thread_ts") != ts
    if not is_reply and int(original.get("reply_count") or 0) > 0:
        raise SlackOpsError(
            f"refusing --replace: message {ts} has thread replies that a repost "
            "would orphan; attach without --replace (thread reply) instead", status=409)
    body = str(text) if text else str(original.get("text") or "")
    if not body.strip():
        raise SlackOpsError(f"message {ts} has no text to repost; pass text", status=400)
    thread_ts = original.get("thread_ts") if is_reply else None
    info, new_ts, lookup_error = _complete(
        token, channel_id, file_id, title, body, thread_ts, sleep)
    if not new_ts:
        raise SlackOpsError(
            f"image reposted (file {info['id']}) but its new message ts could not be "
            f"confirmed ({lookup_error}); the old message {ts} was NOT deleted — "
            "check the channel and delete one copy by hand", status=502)
    _rpc(token, "chat.delete", {"channel": channel_id, "ts": ts})
    return {"ok": True, "mode": "replace", "connection": connection_id,
            "channel": channel_id, "deleted_ts": ts, "ts": new_ts,
            "thread_ts": thread_ts, "file": info}


def _token_scopes(token):
    """The token's OAuth scopes (auth.test's x-oauth-scopes header)."""
    import urllib.request

    request = urllib.request.Request(
        f"{slack_discovery.SLACK_API_URL}/auth.test", data=b"",
        headers={"authorization": f"Bearer {token}"}, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            header = response.headers.get("x-oauth-scopes") or ""
            data = json.loads(response.read().decode() or "{}")
    except Exception as exc:  # noqa: BLE001 — reported without detail that could carry the token
        raise SlackOpsError(f"Slack auth.test unreachable: {type(exc).__name__}",
                            status=502) from None
    if not data.get("ok"):
        raise SlackOpsError(
            f"Slack rejected auth.test: {oauth_providers.redact(str(data.get('error')))}",
            status=502)
    return sorted(s.strip() for s in header.split(",") if s.strip())
