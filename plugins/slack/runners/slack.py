"""slack actions: post a templated message through a stored credential
(top-level or threaded via ``thread_ts``, or scheduled for later via
chat.scheduleMessage), look up a workspace user or channel before acting
(slack_find / slack_find_user), edit a message, react to one, pin one,
search messages, invite users into a channel, set a reminder, send a direct
message, create a channel, set a channel's topic or purpose, and upload a
file into a channel (files.uploadV2)."""
import json
import mimetypes
import os
import re
from urllib.parse import urlencode

from src.dapier.connections import credentials
from src.dapier.engine import logic_pkg as logic
from src.dapier.engine.actions import base
from src.dapier.engine.actions import telegram_format
from src.dapier.engine.actions.templating import render
from src.dapier.engine.actions.base import flag

SLACK_LOOKUP_BY_EMAIL_URL = "https://slack.com/api/users.lookupByEmail"
SLACK_CONVERSATIONS_LIST_URL = "https://slack.com/api/conversations.list"
SLACK_CONVERSATIONS_OPEN_URL = "https://slack.com/api/conversations.open"
SLACK_CONVERSATIONS_CREATE_URL = "https://slack.com/api/conversations.create"
SLACK_CONVERSATIONS_SET_TOPIC_URL = "https://slack.com/api/conversations.setTopic"
SLACK_CONVERSATIONS_SET_PURPOSE_URL = "https://slack.com/api/conversations.setPurpose"
SLACK_UPLOAD_URL_URL = "https://slack.com/api/files.getUploadURLExternal"
SLACK_UPLOAD_COMPLETE_URL = "https://slack.com/api/files.completeUploadExternal"
SLACK_SCHEDULE_URL = "https://slack.com/api/chat.scheduleMessage"
SLACK_INVITE_URL = "https://slack.com/api/conversations.invite"
SLACK_PIN_URL = "https://slack.com/api/pins.add"
SLACK_SEARCH_URL = "https://slack.com/api/search.messages"
SLACK_REMINDERS_ADD_URL = "https://slack.com/api/reminders.add"
_CHANNEL_PAGES = 3
# Slack channel names: lowercase letters, numbers, hyphens, underscores,
# at most 80 characters.
_CHANNEL_NAME_BAD_CHARS = re.compile(r"[^a-z0-9_-]")


def _connection_credential_id(connection_id):
    """The credential id the named Slack connection stores its token under."""
    import boto3

    item = boto3.resource("dynamodb").Table(os.environ["CONNECTIONS_TABLE"]).get_item(
        Key={"connection_id": connection_id},
    ).get("Item") or {}
    return item.get("credential_id")


def _token_for(action):
    if not action.get("connection_id"):
        raise ValueError("Slack action needs a connection_id")
    credential_id = _connection_credential_id(action["connection_id"])
    if not credential_id:
        raise ValueError(f"Slack connection {action['connection_id']!r} has no credential")
    secret = credentials.get_credential(credential_id)
    token = secret.get("token") or secret.get("access_token")
    if not token:
        raise ValueError("Slack secret does not contain a bot token")
    return token


def run_slack(action, event, *, steps=None):
    token = _token_for(action)
    if action.get("telegram_format"):
        return _post_telegram_format(action, event, token)
    text = render(action.get("text", "{title}\n{url}"), event, steps)
    payload = {
        "channel": action["channel"],
        "text": text,
        "unfurl_links": action.get("unfurl_links", True),
        "unfurl_media": action.get("unfurl_media", True),
    }
    if action.get("username"):
        payload["username"] = render(str(action["username"]), event, steps)
    for key in ("link_names", "reply_broadcast"):
        if key in action:
            value = action[key]
            payload[key] = value.strip().lower() == "true" if isinstance(value, str) else bool(value)
    thread_ts = render(str(action.get("thread_ts") or ""), event, steps).strip()
    if thread_ts:
        payload["thread_ts"] = thread_ts
    result = base._json_request(
        "https://slack.com/api/chat.postMessage",
        payload,
        headers={"authorization": f"Bearer {token}"},
        timeout=action.get("timeout_seconds", 10),
    )
    if not result.get("ok"):
        raise RuntimeError(f"Slack rejected message: {result.get('error', 'unknown_error')}")
    return {"ok": True, "channel": result.get("channel"), "ts": result.get("ts")}


def _slack_rpc(url, token, payload, *, transport=None, method="POST"):
    """One Slack Web API request through the injectable transport.

    Unlike ``run_slack`` (which sits on ``base._json_request`` with no seam),
    this takes ``transport`` so tests can fake the network. Returns the parsed
    body; transport failures and HTTP >= 300 raise RuntimeError with the
    Slack error code when the body carries one.
    """
    transport = transport or base._default_transport
    headers = {"authorization": f"Bearer {token}"}
    body = None
    if method == "GET":
        url = f"{url}?{urlencode(payload)}"
    else:
        headers["content-type"] = "application/json"
        body = json.dumps(payload).encode()
    try:
        status, raw = transport(
            method, url, headers=headers, body=body,
            timeout=15,
        )
    except Exception as exc:
        raise RuntimeError(f"slack call unreachable: {type(exc).__name__}")
    try:
        data = json.loads(raw.decode() or "{}")
    except (ValueError, UnicodeDecodeError):
        raise RuntimeError(f"slack returned HTTP {status} with an unreadable body")
    if status >= 300:
        error = data.get("error") or "unknown_error"
        raise RuntimeError(f"slack returned HTTP {status}: {error}")
    return data


def run_slack_find(action, event, steps=None, transport=None):
    """Look up one workspace user or channel; a miss is a result, not an error.

    ``find`` selects the kind (default ``user``): a user is looked up by
    email via ``users.lookupByEmail``; a channel is matched case-insensitively
    (leading ``#`` ignored) across up to three ``conversations.list`` pages.
    Slack answers ``ok: false`` with ``users_not_found`` for an unknown email,
    which becomes ``{"found": False, "user": None}``; any other Slack error
    raises RuntimeError with the error code. ``create_if_missing`` applies to
    the channel branch only (Zapier's Find or Create Channel): a missed name
    is created via conversations.create (``is_private`` honored) and the
    output reports ``created: True``; a user miss stays a miss.
    """
    token = _token_for(action)
    find = str(action.get("find") or "user").strip().lower() or "user"
    if find not in ("user", "channel"):
        raise ValueError(f"slack find must be 'user' or 'channel', not {find!r}")
    query = render(action.get("query", ""), event, steps).strip()
    if not query:
        raise ValueError("slack_find requires a rendered query")

    if find == "user":
        data = _slack_rpc(SLACK_LOOKUP_BY_EMAIL_URL, token, {"email": query},
                          transport=transport)
        if data.get("ok"):
            user = data.get("user") or {}
            profile = user.get("profile") or {}
            return {"found": True, "user": {
                "id": user.get("id"),
                "name": user.get("name"),
                "real_name": user.get("real_name"),
                "email": profile.get("email"),
                "tz": user.get("tz"),
            }}
        if data.get("error") == "users_not_found":
            return {"found": False, "user": None}
        raise RuntimeError(f"Slack find failed: {data.get('error', 'unknown_error')}")

    needle = query.lstrip("#").lower()
    cursor = None
    for _page in range(_CHANNEL_PAGES):
        payload = {"limit": 200, "exclude_archived": True}
        if cursor:
            payload["cursor"] = cursor
        data = _slack_rpc(SLACK_CONVERSATIONS_LIST_URL, token, payload,
                          transport=transport)
        if not data.get("ok"):
            raise RuntimeError(f"Slack find failed: {data.get('error', 'unknown_error')}")
        for channel in data.get("channels") or []:
            if not isinstance(channel, dict):
                continue
            name = str(channel.get("name") or "").lstrip("#").lower()
            if name and name == needle:
                return {"found": True, "channel": {
                    "id": channel.get("id"),
                    "name": channel.get("name"),
                    "is_private": bool(channel.get("is_private")),
                }}
        cursor = (data.get("response_metadata") or {}).get("next_cursor")
        if not cursor:
            break
    if not flag(action, "create_if_missing"):
        return {"found": False, "channel": None}
    # Find-or-create channel (Zapier's pairing): the missed query is the name
    # to reserve, normalized exactly like slack_create_channel normalizes.
    name = _normalize_channel_name(needle)
    if not name:
        return {"found": False, "channel": None}
    data = _slack_rpc(SLACK_CONVERSATIONS_CREATE_URL, token,
                      {"name": name, "is_private": flag(action, "is_private")},
                      transport=transport)
    if not data.get("ok"):
        raise RuntimeError(f"Slack create-channel failed: {data.get('error', 'unknown_error')}")
    channel = data.get("channel") or {}
    return {"found": True, "created": True, "channel": {
        "id": channel.get("id"),
        "name": channel.get("name") or name,
        "is_private": bool(channel.get("is_private", flag(action, "is_private"))),
    }}


def run_slack_find_user(action, event, *, steps=None, transport=None):
    """Look up one workspace user by email via ``users.lookupByEmail``.

    The single-purpose counterpart of :func:`run_slack_find`'s user branch,
    for find-then-act recipes: ``email`` is rendered from the event, a miss
    (Slack's ``users_not_found``) is ``{"found": False, "user": None}`` and
    any other Slack error raises RuntimeError with the error code.
    """
    token = _token_for(action)
    email = render(action.get("email", ""), event, steps).strip()
    if not email:
        raise ValueError("slack_find_user requires a rendered email")
    data = _slack_rpc(SLACK_LOOKUP_BY_EMAIL_URL, token, {"email": email},
                      transport=transport)
    if data.get("ok"):
        user = data.get("user") or {}
        profile = user.get("profile") or {}
        return {"found": True, "user": {
            "id": user.get("id"),
            "name": user.get("name"),
            "real_name": user.get("real_name"),
            "email": profile.get("email"),
            "tz": user.get("tz"),
        }}
    if data.get("error") == "users_not_found":
        return {"found": False, "user": None}
    raise RuntimeError(f"Slack find failed: {data.get('error', 'unknown_error')}")


def run_slack_update_message(action, event, *, steps=None, transport=None):
    """Edit one already-posted message (``chat.update``).

    ``channel`` and ``ts`` are rendered from the event — the trigger envelope
    of ``slack`` events carries both (``channel_id``, ``ts``) — and ``text``
    replaces the message body. Slack answers the edit with the channel and
    ts; an unknown or too-old message raises RuntimeError with Slack's error
    code (``message_not_found``, ``cant_update_message``).
    """
    token = _token_for(action)
    channel = render(action.get("channel", ""), event, steps).strip()
    ts = render(action.get("ts", ""), event, steps).strip()
    text = render(action.get("text", ""), event, steps)
    if not channel or not ts:
        raise ValueError("slack_update_message requires channel and ts — e.g. "
                         "{channel_id} and {ts} from a slack trigger")
    data = _slack_rpc("https://slack.com/api/chat.update", token,
                      {"channel": channel, "ts": ts, "text": text},
                      transport=transport)
    if not data.get("ok"):
        raise RuntimeError(f"Slack update failed: {data.get('error', 'unknown_error')}")
    return {"ok": True, "channel": data.get("channel"), "ts": data.get("ts")}


def run_slack_add_reaction(action, event, *, steps=None, transport=None):
    """React to one message (``reactions.add``).

    ``reaction`` is the emoji name without colons (``tada``, not ``:tada:``);
    ``channel`` + ``timestamp`` point at the message (``channel_id``/``ts``
    of a slack trigger envelope). Reacting twice is not an error — Slack's
    ``already_reacted`` comes back as ``{"ok": True, "already_reacted":
    True}`` so a retried run stays green; any other Slack error raises
    RuntimeError with the error code.
    """
    token = _token_for(action)
    channel = render(action.get("channel", ""), event, steps).strip()
    timestamp = render(action.get("timestamp", ""), event, steps).strip()
    name = render(action.get("reaction", ""), event, steps).strip().strip(":")
    if not channel or not timestamp or not name:
        raise ValueError("slack_add_reaction requires channel, timestamp and a "
                         "reaction name like 'tada' (colons optional)")
    data = _slack_rpc("https://slack.com/api/reactions.add", token,
                      {"channel": channel, "timestamp": timestamp, "name": name},
                      transport=transport)
    if not data.get("ok"):
        if data.get("error") == "already_reacted":
            return {"ok": True, "reaction": name, "channel": channel,
                    "ts": timestamp, "already_reacted": True}
        raise RuntimeError(f"Slack reaction failed: {data.get('error', 'unknown_error')}")
    return {"ok": True, "reaction": name, "channel": channel, "ts": timestamp}


def run_slack_dm(action, event, *, steps=None, transport=None):
    """Send a direct message to one user (conversations.open +
    chat.postMessage, Zapier's Send Direct Message).

    ``user_id`` is rendered from the event — chain slack_find_user and pass
    ``{steps.<id>.output.user.id}`` to target the person an event names.
    ``conversations.open`` reuses the existing DM channel when there is one
    (with ``return_im`` so the channel id is stable), the text posts into it
    with the same unfurl defaults as the channel action. Output:
    ``{ok: True, user, channel, ts}``.
    """
    token = _token_for(action)
    user_id = render(action.get("user_id", ""), event, steps).strip()
    if not user_id:
        raise ValueError("slack_dm requires a rendered user_id")
    opened = _slack_rpc(SLACK_CONVERSATIONS_OPEN_URL, token,
                        {"users": user_id, "return_im": True},
                        transport=transport)
    if not opened.get("ok"):
        raise RuntimeError(f"Slack DM open failed: {opened.get('error', 'unknown_error')}")
    channel = (opened.get("channel") or {}).get("id")
    if not channel:
        raise RuntimeError("slack conversations.open returned no DM channel")
    text = render(action.get("text", ""), event, steps)
    result = _slack_rpc("https://slack.com/api/chat.postMessage", token,
                        {"channel": channel, "text": text,
                         "unfurl_links": action.get("unfurl_links", True),
                         "unfurl_media": action.get("unfurl_media", True)},
                        transport=transport)
    if not result.get("ok"):
        raise RuntimeError(f"Slack rejected message: {result.get('error', 'unknown_error')}")
    return {"ok": True, "user": user_id, "channel": result.get("channel") or channel,
            "ts": result.get("ts")}


def _schedule_epoch(raw):
    """post_at as epoch seconds: an ISO 8601 datetime through the engine's
    ``logic.parse_moment`` (a trailing Z or a missing offset reads as UTC)
    or a bare epoch-seconds number passed through unchanged."""
    text = str(raw or "").strip()
    if not text:
        raise ValueError("slack_schedule_message requires post_at — an ISO "
                         "datetime or epoch seconds")
    if re.fullmatch(r"-?\d+", text):
        return int(text)
    epoch = logic.parse_moment(text)
    if epoch is None:
        raise ValueError(f"slack_schedule_message could not read post_at "
                         f"{text!r}: use an ISO 8601 datetime or epoch seconds")
    return int(epoch)


def run_slack_schedule_message(action, event, *, steps=None, transport=None):
    """Schedule one message for later (chat.scheduleMessage, Zapier's Send
    Scheduled Message).

    ``channel`` and ``text`` render from the event; ``post_at`` is templated
    too and takes an ISO 8601 datetime or epoch seconds. Optional
    ``thread_ts`` schedules the message as a reply in that thread. Output:
    ``{ok: True, channel, scheduled_message_id, ts, post_at}`` where
    ``post_at`` is the epoch seconds actually sent.
    """
    token = _token_for(action)
    channel = render(action.get("channel", ""), event, steps).strip()
    text = render(action.get("text", ""), event, steps)
    if not channel:
        raise ValueError("slack_schedule_message requires a channel")
    if not text.strip():
        raise ValueError("slack_schedule_message requires text")
    post_at = _schedule_epoch(render(str(action.get("post_at") or ""), event, steps))
    payload = {
        "channel": channel,
        "text": text,
        "post_at": post_at,
        "unfurl_links": action.get("unfurl_links", True),
        "unfurl_media": action.get("unfurl_media", True),
    }
    thread_ts = render(str(action.get("thread_ts") or ""), event, steps).strip()
    if thread_ts:
        payload["thread_ts"] = thread_ts
    data = _slack_rpc(SLACK_SCHEDULE_URL, token, payload, transport=transport)
    if not data.get("ok"):
        raise RuntimeError(f"Slack schedule failed: {data.get('error', 'unknown_error')}")
    return {"ok": True, "channel": data.get("channel") or channel,
            "scheduled_message_id": data.get("scheduled_message_id"),
            "ts": data.get("ts"),
            "post_at": post_at}


def run_slack_add_reminder(action, event, *, steps=None, transport=None):
    """Set one reminder (reminders.add, Zapier's Add Reminder).

    ``text`` is rendered from the event; ``time`` is optional and takes
    Slack's natural-language times (``in 20 minutes``, ``tomorrow 9am``)
    or epoch seconds — a bare number is sent as epoch so a rendered
    ``{steps.<id>.output.*}`` timestamp reads as a moment, not a phrase.
    With no ``time``, Slack reminds in 20 minutes. The reminder belongs to
    the token's own user (reminders.add cannot target someone else).
    Output: ``{ok: True, reminder: {id, time, text}}``; any other Slack
    error raises RuntimeError with the error code.
    """
    token = _token_for(action)
    text = render(action.get("text", ""), event, steps).strip()
    if not text:
        raise ValueError("slack_add_reminder requires rendered text")
    payload = {"text": text}
    when = render(str(action.get("time") or ""), event, steps).strip()
    if when:
        payload["time"] = int(when) if re.fullmatch(r"-?\d+", when) else when
    data = _slack_rpc(SLACK_REMINDERS_ADD_URL, token, payload, transport=transport)
    if not data.get("ok"):
        raise RuntimeError(f"Slack reminder failed: {data.get('error', 'unknown_error')}")
    reminder = data.get("reminder") or {}
    return {"ok": True, "reminder": {
        "id": reminder.get("id"),
        "time": reminder.get("time"),
        "text": reminder.get("text") or text,
    }}


def _normalize_channel_name(raw):
    """A name Slack accepts: lowercased, spaces to hyphens, characters
    outside [a-z0-9_-] dropped, hyphen edges stripped, 80 characters max."""
    name = str(raw or "").strip().lower().replace(" ", "-")
    name = _CHANNEL_NAME_BAD_CHARS.sub("", name).strip("-")
    return name[:80]


def run_slack_create_channel(action, event, *, steps=None, transport=None):
    """Create one channel (conversations.create, Zapier's Create Channel).

    ``name`` is rendered from the event and normalized to what Slack accepts
    (lowercase, spaces to hyphens, illegal characters dropped, 80 characters)
    so templated names like ``alerts-{customer}`` never fail on a stray
    character. ``is_private`` creates a private channel. An existing name is
    Slack's ``name_taken`` error — chain slack_find first when
    create-if-missing matters. Output: ``{ok: True, channel: {id, name,
    is_private}}``.
    """
    token = _token_for(action)
    name = _normalize_channel_name(render(action.get("name", ""), event, steps))
    if not name:
        raise ValueError("slack_create_channel requires a channel name")
    is_private = bool(action.get("is_private"))
    data = _slack_rpc(SLACK_CONVERSATIONS_CREATE_URL, token,
                      {"name": name, "is_private": is_private},
                      transport=transport)
    if not data.get("ok"):
        raise RuntimeError(f"Slack create-channel failed: {data.get('error', 'unknown_error')}")
    channel = data.get("channel") or {}
    return {"ok": True, "channel": {
        "id": channel.get("id"),
        "name": channel.get("name") or name,
        "is_private": bool(channel.get("is_private", is_private)),
    }}


def _run_slack_set_channel_field(action, event, *, url, key, verb, steps, transport):
    """conversations.setTopic / conversations.setPurpose — one shape.

    ``channel`` is the channel id (rendered; the slack trigger envelope's
    ``channel_id`` works), ``value`` the rendered text to set. Output:
    ``{ok: True, channel, <key>: value}``.
    """
    token = _token_for(action)
    channel = render(action.get("channel", ""), event, steps).strip()
    value = render(action.get(key, ""), event, steps).strip()
    if not channel:
        raise ValueError(f"slack_{verb} requires a channel")
    if not value:
        raise ValueError(f"slack_{verb} requires {key}")
    data = _slack_rpc(url, token, {"channel": channel, key: value},
                      transport=transport)
    if not data.get("ok"):
        raise RuntimeError(f"Slack {verb.replace('_', ' ')} failed: "
                           f"{data.get('error', 'unknown_error')}")
    return {"ok": True, "channel": data.get("channel") or channel, key: value}


def run_slack_set_topic(action, event, *, steps=None, transport=None):
    """Set one channel's topic (conversations.setTopic, Zapier's Set Channel
    Topic). Output: ``{ok: True, channel, topic}``."""
    return _run_slack_set_channel_field(
        action, event, url=SLACK_CONVERSATIONS_SET_TOPIC_URL, key="topic",
        verb="set_topic", steps=steps, transport=transport)


def run_slack_set_purpose(action, event, *, steps=None, transport=None):
    """Set one channel's purpose (conversations.setPurpose, Zapier's Set
    Channel Purpose). Output: ``{ok: True, channel, purpose}``."""
    return _run_slack_set_channel_field(
        action, event, url=SLACK_CONVERSATIONS_SET_PURPOSE_URL, key="purpose",
        verb="set_purpose", steps=steps, transport=transport)


def run_slack_invite_to_channel(action, event, *, steps=None, transport=None):
    """Invite one or more workspace users into a channel (conversations.invite).

    ``users`` takes comma-separated member ids — pick one from the user
    picker or chain slack_find_user and pass
    ``{steps.<id>.output.user.id}``. Slack answers ``ok: false`` with
    ``already_in_channel`` for a user who is already a member; that is
    absorbed as ``invited: False`` carrying the ``reason`` (the same
    benign-error shape slack_add_reaction uses for ``already_reacted``) so a
    retried run stays green; any other Slack error raises RuntimeError with
    the error code. Output: ``{invited, channel, users}`` — plus ``reason``
    on the absorbed miss.
    """
    token = _token_for(action)
    channel = render(action.get("channel", ""), event, steps).strip()
    users = render(action.get("users", ""), event, steps).strip()
    if not channel or not users:
        raise ValueError("slack_invite_to_channel requires channel and users "
                         "(comma-separated member ids)")
    data = _slack_rpc(SLACK_INVITE_URL, token,
                      {"channel": channel, "users": users},
                      transport=transport)
    if not data.get("ok"):
        if data.get("error") == "already_in_channel":
            return {"invited": False, "channel": channel, "users": users,
                    "reason": "already_in_channel"}
        raise RuntimeError(f"Slack invite failed: {data.get('error', 'unknown_error')}")
    return {"invited": True, "channel": channel, "users": users}


def run_slack_pin_message(action, event, *, steps=None, transport=None):
    """Pin one message to its channel (pins.add, Zapier's Pin Message).

    ``channel`` + ``timestamp`` point at the message (``channel_id``/``ts``
    of a slack trigger envelope). Output: ``{pinned: True, channel,
    timestamp}``; any Slack error raises RuntimeError with the error code.
    """
    token = _token_for(action)
    channel = render(action.get("channel", ""), event, steps).strip()
    timestamp = render(action.get("timestamp", ""), event, steps).strip()
    if not channel or not timestamp:
        raise ValueError("slack_pin_message requires channel and timestamp — "
                         "e.g. {channel_id} and {ts} from a slack trigger")
    data = _slack_rpc(SLACK_PIN_URL, token,
                      {"channel": channel, "timestamp": timestamp},
                      transport=transport)
    if not data.get("ok"):
        raise RuntimeError(f"Slack pin failed: {data.get('error', 'unknown_error')}")
    return {"pinned": True, "channel": channel, "timestamp": timestamp}


def _search_count(raw):
    """``count`` clamped to search.messages's 1-100 page bound, default 20."""
    try:
        number = int(str(raw).strip())
    except (TypeError, ValueError):
        return 20
    return max(1, min(100, number))


def run_slack_find_message(action, event, *, steps=None, transport=None):
    """Search workspace messages (search.messages, Zapier's Find Message).

    ``query`` searches like Slack's own search box; ``count`` caps the
    results (1-100, default 20). The token needs the ``search:read`` scope.
    A miss is a verdict, not an error — ``{found: False, messages: [],
    count: 0}``, following slack_find's found/not-found style; any other
    Slack error raises RuntimeError with the error code. Output on a hit:
    ``{found: True, messages: [{ts, channel_id, channel_name, user, text,
    permalink}], count}``.
    """
    token = _token_for(action)
    query = render(action.get("query", ""), event, steps).strip()
    if not query:
        raise ValueError("slack_find_message requires a rendered query")
    data = _slack_rpc(SLACK_SEARCH_URL, token,
                      {"query": query, "count": _search_count(action.get("count"))},
                      transport=transport, method="GET")
    if not data.get("ok"):
        raise RuntimeError(f"Slack find failed: {data.get('error', 'unknown_error')}")
    matches = [match for match in (data.get("messages") or {}).get("matches") or []
               if isinstance(match, dict)]
    messages = [{
        "ts": match.get("ts"),
        "channel_id": (match.get("channel") or {}).get("id"),
        "channel_name": (match.get("channel") or {}).get("name"),
        "user": match.get("user") or match.get("username"),
        "text": match.get("text"),
        "permalink": match.get("permalink"),
    } for match in matches]
    return {"found": bool(messages), "messages": messages, "count": len(messages)}


# --- slack_upload_file: one file into a channel (files.uploadV2) --------------


def _file_bytes(action, event, steps, *, transport=None):
    """The uploaded bytes: an HTTP download, a staged S3 object, or inline
    text — exactly one, mirroring drive_upload_file's source composition.

    ``source_s3`` bucket/key take templates, so an earlier step's staged
    file (dropbox_read_file's or drive_read_file's output) uploads without
    a literal bucket/key in the workflow; inline ``content`` takes
    templates too.
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
        raise ValueError("slack_upload_file takes one content source "
                         f"({', '.join(given)} given): source_url, source_s3, or content")
    if url:
        try:
            status, body = transport("GET", url, headers={}, body=None, timeout=30)
        except Exception as exc:
            raise RuntimeError(f"file download unreachable: {type(exc).__name__}") from None
        if status >= 300:
            raise RuntimeError(f"file download returned HTTP {status}")
        return body
    if staged["bucket"] and staged["key"]:
        return base._s3_body(staged)
    if inline:
        return inline.encode()
    raise ValueError("slack_upload_file needs source_url, source_s3 "
                     "with bucket and key, or content")


def run_slack_upload_file(action, event, *, steps=None, transport=None):
    """Send one file into a channel (Zapier's Send File), through Slack's
    modern three-step upload: ``files.getUploadURLExternal`` mints a
    presigned URL for the bytes, the binary POST fills it (no bearer
    header — the URL is the credential), ``files.completeUploadExternal``
    publishes the file into the channel, optionally with a comment and
    threaded under ``thread_ts``.

    The bytes come from exactly one content source — ``source_url`` (an
    HTTP download), ``source_s3`` ``{bucket, key}`` (a staged object), or
    inline ``content`` — so a dropbox/drive read-file step chains straight
    into this one. ``content_type`` defaults to a guess from the file
    name. Output: ``{ok: True, channel, file: {id, name, title,
    permalink}}``.
    """
    token = _token_for(action)
    transport = transport or base._default_transport
    channel = render(action.get("channel", ""), event, steps).strip()
    raw_name = render(action.get("filename", ""), event, steps).strip()
    if not channel:
        raise ValueError("slack_upload_file requires a channel")
    if not raw_name:
        raise ValueError("slack_upload_file requires a filename")
    filename = base._safe_filename(raw_name)
    body = _file_bytes(action, event, steps, transport=transport)
    content_type = (render(str(action.get("content_type") or ""), event, steps).strip()
                    or mimetypes.guess_type(filename)[0]
                    or "application/octet-stream")
    requested = _slack_rpc(SLACK_UPLOAD_URL_URL, token,
                           {"filename": filename, "length": len(body),
                            "content_type": content_type},
                           transport=transport)
    if not requested.get("ok"):
        raise RuntimeError(f"Slack upload failed: {requested.get('error', 'unknown_error')}")
    upload_url = str(requested.get("upload_url") or "")
    file_id = str(requested.get("file_id") or "")
    if not upload_url or not file_id:
        raise RuntimeError("slack upload returned no upload target")
    try:
        status, _raw = transport("POST", upload_url,
                                 headers={"content-type": content_type},
                                 body=body, timeout=60)
    except Exception as exc:
        raise RuntimeError(f"slack file upload unreachable: {type(exc).__name__}") from None
    if status >= 300:
        raise RuntimeError(f"slack file upload returned HTTP {status}")
    title = render(str(action.get("title") or ""), event, steps).strip() or filename
    completion = {"files": [{"id": file_id, "title": title}],
                  "channel_id": channel}
    comment = render(str(action.get("initial_comment") or ""), event, steps).strip()
    if comment:
        completion["initial_comment"] = comment
    thread_ts = render(str(action.get("thread_ts") or ""), event, steps).strip()
    if thread_ts:
        completion["thread_ts"] = thread_ts
    finished = _slack_rpc(SLACK_UPLOAD_COMPLETE_URL, token, completion,
                          transport=transport)
    if not finished.get("ok"):
        raise RuntimeError(f"Slack upload failed: {finished.get('error', 'unknown_error')}")
    files = [entry for entry in finished.get("files") or [] if isinstance(entry, dict)]
    first = files[0] if files else {}
    return {
        "ok": True,
        "channel": channel,
        "file": {
            "id": first.get("id") or file_id,
            "name": first.get("name") or filename,
            "title": first.get("title") or title,
            "permalink": first.get("permalink"),
        },
    }


# Telegram attachment kinds the crosspost mirrors, in the order a message is
# checked. ``photo`` is a list of sizes; the others are one file object.
TELEGRAM_FILE_KINDS = ("document", "video", "animation", "audio", "voice", "video_note")

# The Bot API's getFile serves files up to 20 MB; bigger ones cannot be
# downloaded by the bot at all.
TELEGRAM_DOWNLOAD_LIMIT = 20 * 1024 * 1024

# A Slack file is processed asynchronously after completeUploadExternal; an
# image block that references it can be refused (invalid_blocks) until then.
SLACK_FILE_BLOCK_RETRY_DELAYS = (1, 2, 4)

# The image of a public channel post, as the t.me embed renders it.
TELEGRAM_PREVIEW_IMAGE_RE = re.compile(
    r"tgme_widget_message_photo_wrap[^>]*?background-image:url\('(https://[^']+)'\)")


def _telegram_message(data):
    """The raw Telegram message a crosspost event carries (or {})."""
    update = data.get("update") if isinstance(data.get("update"), dict) else {}
    for key in ("channel_post", "edited_channel_post", "message", "edited_message"):
        if isinstance(update.get(key), dict):
            return update[key]
    return {}


def _telegram_media(message):
    """The attachment of a Telegram message: ``{kind, file_id, filename,
    size, mime_type, image}``, or None for a text-only post.

    A photo arrives as several sizes; the largest one is mirrored.
    """
    message_id = message.get("message_id") or "post"
    photos = message.get("photo")
    if isinstance(photos, list):
        sizes = [size for size in photos if isinstance(size, dict) and size.get("file_id")]
        if sizes:
            best = max(sizes, key=lambda size: ((size.get("width") or 0) * (size.get("height") or 0),
                                                size.get("file_size") or 0))
            return {"kind": "photo", "file_id": best["file_id"],
                    "filename": f"telegram-{message_id}.jpg", "size": best.get("file_size"),
                    "mime_type": "image/jpeg", "image": True}
    for kind in TELEGRAM_FILE_KINDS:
        item = message.get(kind)
        if not isinstance(item, dict) or not item.get("file_id"):
            continue
        filename = base._safe_filename(str(item.get("file_name") or f"telegram-{message_id}-{kind}"))
        mime_type = (item.get("mime_type") or mimetypes.guess_type(filename)[0]
                     or "application/octet-stream")
        return {"kind": kind, "file_id": item["file_id"], "filename": filename,
                "size": item.get("file_size"), "mime_type": mime_type,
                "image": kind == "document" and mime_type.startswith("image/")}
    return None


def _telegram_bot_token(action, data):
    """The bot token that can download the post's files.

    A file_id only works for the bot that received it, so this is the
    trigger's own bot: ``telegram_connection_id`` on the action, else the
    connection of the telegram trigger (``data.hook``) the event came from.
    """
    from src.dapier.triggers import hook_triggers

    connection_id = str(action.get("telegram_connection_id") or "").strip()
    if not connection_id and data.get("hook"):
        item = hook_triggers.get_item(str(data["hook"])) or {}
        connection_id = str(item.get("connection_id") or "").strip()
    if not connection_id:
        raise RuntimeError("no telegram connection to download the attachment with")
    connection = base._connected_connection(connection_id)
    token = credentials.get_credential(connection["credential_id"]).get("token")
    if not token:
        raise RuntimeError(f"telegram connection {connection_id!r} has no bot token")
    return token


def _telegram_download(token, media, *, transport):
    """The attachment's bytes through getFile. Errors never carry the token
    (the Bot API puts it in the URL path)."""
    if (media.get("size") or 0) > TELEGRAM_DOWNLOAD_LIMIT:
        raise RuntimeError("attachment is over the Bot API's 20 MB download limit")
    query = urlencode({"file_id": media["file_id"]})
    try:
        status, raw = transport("GET", f"https://api.telegram.org/bot{token}/getFile?{query}",
                                headers={}, body=None, timeout=15)
        info = json.loads(raw.decode() or "{}")
    except Exception as exc:
        raise RuntimeError(f"telegram getFile failed: {type(exc).__name__}") from None
    file_path = ((info.get("result") or {}).get("file_path")
                 if isinstance(info, dict) and info.get("ok") else None)
    if status >= 300 or not file_path:
        description = info.get("description") if isinstance(info, dict) else None
        raise RuntimeError(f"telegram getFile failed: {description or f'HTTP {status}'}")
    try:
        status, body = transport("GET", f"https://api.telegram.org/file/bot{token}/{file_path}",
                                 headers={}, body=None, timeout=60)
    except Exception as exc:
        raise RuntimeError(f"telegram file download failed: {type(exc).__name__}") from None
    if status >= 300:
        raise RuntimeError(f"telegram file download returned HTTP {status}")
    return body


def _slack_upload(token, media, body, *, transport, channel_id=None, thread_ts=None):
    """Upload bytes to Slack (getUploadURLExternal, POST, completeUpload).

    Without ``channel_id`` the file stays unshared, for an image block to
    reference by id; with it, the file is shared there (under ``thread_ts``).
    Needs the ``files:write`` scope. Returns the Slack file id.
    """
    requested = _slack_rpc(SLACK_UPLOAD_URL_URL, token,
                           {"filename": media["filename"], "length": len(body)},
                           transport=transport, method="GET")
    if not requested.get("ok"):
        raise RuntimeError(f"Slack upload failed: {requested.get('error', 'unknown_error')}")
    upload_url = str(requested.get("upload_url") or "")
    file_id = str(requested.get("file_id") or "")
    if not upload_url or not file_id:
        raise RuntimeError("slack upload returned no upload target")
    try:
        status, _raw = transport("POST", upload_url,
                                 headers={"content-type": media["mime_type"]},
                                 body=body, timeout=60)
    except Exception as exc:
        raise RuntimeError(f"slack file upload unreachable: {type(exc).__name__}") from None
    if status >= 300:
        raise RuntimeError(f"slack file upload returned HTTP {status}")
    completion = {"files": json.dumps([{"id": file_id, "title": media["filename"]}])}
    if channel_id:
        completion["channel_id"] = channel_id
    if thread_ts:
        completion["thread_ts"] = thread_ts
    try:
        status, raw = transport(
            "POST", SLACK_UPLOAD_COMPLETE_URL,
            headers={"authorization": f"Bearer {token}",
                     "content-type": "application/x-www-form-urlencoded"},
            body=urlencode(completion).encode(), timeout=15)
        finished = json.loads(raw.decode() or "{}")
    except Exception as exc:
        raise RuntimeError(f"slack upload completion failed: {type(exc).__name__}") from None
    if not finished.get("ok"):
        raise RuntimeError(f"Slack upload failed: {finished.get('error', 'unknown_error')}")
    return file_id


def _telegram_preview_image(data, *, transport):
    """The post's image as the public t.me embed serves it (a CDN URL Slack
    can fetch), or None. The fallback when the file cannot be uploaded."""
    username = str((data.get("chat") or {}).get("username") or "").strip()
    message_id = data.get("message_id")
    if not username or message_id is None:
        return None
    try:
        status, raw = transport("GET", f"https://t.me/{username}/{message_id}?embed=1&mode=tme",
                                headers={}, body=None, timeout=15)
    except Exception:
        return None
    if status >= 300:
        return None
    match = TELEGRAM_PREVIEW_IMAGE_RE.search(raw.decode("utf-8", "replace"))
    return match.group(1) if match else None


def _alt_text(text):
    plain = " ".join(str(text or "").split())
    return (plain[:200] if plain else "Image from the Telegram post")


def _post_telegram_format(action, event, token, *, transport=None, sleep=None):
    """Post a Telegram update as rendered Slack blocks (au-tomator behavior).

    The first message goes to the channel; anything that does not fit — plus
    the rendered ``source_link`` — continues in its thread. The body comes
    from the event's ``text``/``entities`` fields (the caption for a media
    post), not the ``text`` template: rendering the raw text through a
    template would drop the entity markup the blocks are built from.

    A post with an attachment mirrors the attachment itself. An image (a
    photo, or an image sent as a file) is downloaded with the trigger's bot,
    uploaded to Slack and shown as an image block in the first message under
    the caption; when the upload is refused (no ``files:write`` scope) the
    public t.me preview image is shown instead. Any other file is uploaded
    into the post's thread. A post with neither text nor an attachment the
    crosspost can show is skipped: it never posts an empty placeholder.
    """
    import time

    transport = transport or base._default_transport
    sleep = sleep or time.sleep
    data = event.get("data", {})
    text = data.get("text") or ""
    media = _telegram_media(_telegram_message(data))
    link = render(action.get("source_link", ""), event, steps=None).strip()
    channel = action["channel"]
    messages = telegram_format.build_messages(text, data.get("entities")) if text else [[]]
    first_blocks = list(messages[0])
    output = {"ok": True, "channel": channel}

    body = None
    media_error = None
    if media:
        try:
            body = _telegram_download(_telegram_bot_token(action, data), media,
                                      transport=transport)
        except Exception as exc:  # noqa: BLE001 — reported in the output, then fallback
            media_error = str(exc)

    first = None
    if media and media["image"]:
        alt = _alt_text(text)
        if body is not None:
            try:
                file_id = _slack_upload(token, media, body, transport=transport)
                image = {"type": "image", "slack_file": {"id": file_id}, "alt_text": alt}
                for delay in (*SLACK_FILE_BLOCK_RETRY_DELAYS, None):
                    try:
                        first = _post_blocks(token, action, channel, first_blocks + [image])
                        break
                    except RuntimeError as exc:
                        if delay is None or "invalid_blocks" not in str(exc):
                            raise
                        sleep(delay)
                output["media"] = {"kind": media["kind"], "via": "upload", "file_id": file_id}
            except Exception as exc:  # noqa: BLE001 — fall back to the public preview
                media_error = str(exc)
        if first is None:
            preview = _telegram_preview_image(data, transport=transport)
            if preview:
                image = {"type": "image", "image_url": preview, "alt_text": alt}
                first = _post_blocks(token, action, channel, first_blocks + [image])
                output["media"] = {"kind": media["kind"], "via": "telegram_preview"}
    elif media and body is not None and not first_blocks:
        first_blocks = [{"type": "section", "text": {
            "type": "mrkdwn", "text": f"\U0001f4ce *{telegram_format.escape_mrkdwn(media['filename'])}*"}}]

    if first is None:
        if not first_blocks:
            output.update({"ok": True, "skipped": True, "messages": 0,
                           "reason": "no text and no attachment that could be mirrored"})
            if media_error:
                output["media_error"] = media_error
            return output
        first = _post_blocks(token, action, channel, first_blocks)
    if media_error:
        output["media_error"] = media_error

    thread_ts = first.get("ts")
    posted = 1
    for blocks in messages[1:]:
        _post_blocks(token, action, channel, blocks, thread_ts=thread_ts)
        posted += 1
    if media and not media["image"] and body is not None and thread_ts:
        try:
            file_id = _slack_upload(token, media, body, transport=transport,
                                    channel_id=first.get("channel") or channel,
                                    thread_ts=thread_ts)
            output["media"] = {"kind": media["kind"], "via": "upload", "file_id": file_id}
        except Exception as exc:  # noqa: BLE001 — the text is already posted
            output["media_error"] = str(exc)
    if link and thread_ts:
        _post_blocks(token, action, channel, telegram_format.link_blocks(link),
                     thread_ts=thread_ts)
        posted += 1
    output.update({"ts": thread_ts, "messages": posted})
    return output


def _post_blocks(token, action, channel, blocks, thread_ts=None):
    message = {
        "channel": channel,
        "unfurl_links": False,
        "unfurl_media": False,
        "blocks": blocks,
    }
    if thread_ts is not None:
        message["thread_ts"] = thread_ts
    result = base._json_request(
        "https://slack.com/api/chat.postMessage",
        message,
        headers={"authorization": f"Bearer {token}"},
        timeout=action.get("timeout_seconds", 10),
    )
    if not result.get("ok"):
        raise RuntimeError(f"Slack rejected message: {result.get('error', 'unknown_error')}")
    return result
