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

from src.dapier.connections import credentials
from src.dapier.engine import logic
from src.dapier.engine.actions import base
from src.dapier.engine.actions import telegram_format
from src.dapier.engine.actions.templating import render

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


def _token_for(action):
    credential_id = action.get("credential_id")
    if action.get("connection_id"):
        import boto3

        item = boto3.resource("dynamodb").Table(os.environ["CONNECTIONS_TABLE"]).get_item(
            Key={"connection_id": action["connection_id"]},
        ).get("Item") or {}
        credential_id = item.get("credential_id") or credential_id
    if not credential_id:
        raise ValueError("Slack action needs a connection_id or credential_id")
    secret = credentials.get_credential(credential_id)
    token = (secret.get("token") or secret.get("bot_token")
             or secret.get("user_token") or secret.get("SLACK_BOT_TOKEN"))
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


def _slack_rpc(url, token, payload, *, transport=None):
    """One Slack Web API POST through the injectable transport.

    Unlike ``run_slack`` (which sits on ``base._json_request`` with no seam),
    this takes ``transport`` so tests can fake the network. Returns the parsed
    body; transport failures and HTTP >= 300 raise RuntimeError with the
    Slack error code when the body carries one.
    """
    transport = transport or base._default_transport
    try:
        status, raw = transport(
            "POST", url,
            headers={
                "authorization": f"Bearer {token}",
                "content-type": "application/json",
            },
            body=json.dumps(payload).encode(),
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


def _flag(action, key):
    """Designer boolean fields arrive as "true"/"false" strings."""
    value = action.get(key)
    if isinstance(value, str):
        return value.strip().lower() in ("true", "1", "yes", "on")
    return bool(value)


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
    if not _flag(action, "create_if_missing"):
        return {"found": False, "channel": None}
    # Find-or-create channel (Zapier's pairing): the missed query is the name
    # to reserve, normalized exactly like slack_create_channel normalizes.
    name = _normalize_channel_name(needle)
    if not name:
        return {"found": False, "channel": None}
    data = _slack_rpc(SLACK_CONVERSATIONS_CREATE_URL, token,
                      {"name": name, "is_private": _flag(action, "is_private")},
                      transport=transport)
    if not data.get("ok"):
        raise RuntimeError(f"Slack create-channel failed: {data.get('error', 'unknown_error')}")
    channel = data.get("channel") or {}
    return {"found": True, "created": True, "channel": {
        "id": channel.get("id"),
        "name": channel.get("name") or name,
        "is_private": bool(channel.get("is_private", _flag(action, "is_private"))),
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
                      transport=transport)
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


def _post_telegram_format(action, event, token):
    """Post a Telegram update as rendered Slack blocks (au-tomator behavior).

    The first message goes to the channel; anything that does not fit — plus
    the rendered ``source_link`` — continues in its thread. The body comes
    from the event's ``text``/``entities`` fields, not the ``text`` template:
    rendering the raw text through a template would drop the entity markup
    the blocks are built from.
    """
    data = event.get("data", {})
    text = data.get("text") or "no text in this post, see Telegram for the attachment"
    messages = telegram_format.build_messages(text, data.get("entities"))
    link = render(action.get("source_link", ""), event, steps=None).strip()

    channel = action["channel"]
    first = _post_blocks(token, action, channel, messages[0])
    thread_ts = first.get("ts")
    posted = 1
    for blocks in messages[1:]:
        _post_blocks(token, action, channel, blocks, thread_ts=thread_ts)
        posted += 1
    if link and thread_ts:
        _post_blocks(token, action, channel, telegram_format.link_blocks(link),
                     thread_ts=thread_ts)
        posted += 1
    return {"ok": True, "channel": channel, "ts": thread_ts, "messages": posted}


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
