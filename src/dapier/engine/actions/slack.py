"""slack action: post a templated message through a stored credential,
plus slack_find — look up a workspace user or channel before acting."""
import json
import os

from ...connections import credentials
from . import base
from . import telegram_format
from .templating import render

SLACK_LOOKUP_BY_EMAIL_URL = "https://slack.com/api/users.lookupByEmail"
SLACK_CONVERSATIONS_LIST_URL = "https://slack.com/api/conversations.list"
_CHANNEL_PAGES = 3


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
    result = base._json_request(
        "https://slack.com/api/chat.postMessage",
        {
            "channel": action["channel"],
            "text": text,
            "unfurl_links": action.get("unfurl_links", True),
            "unfurl_media": action.get("unfurl_media", True),
        },
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


def run_slack_find(action, event, steps=None, transport=None):
    """Look up one workspace user or channel; a miss is a result, not an error.

    ``find`` selects the kind (default ``user``): a user is looked up by
    email via ``users.lookupByEmail``; a channel is matched case-insensitively
    (leading ``#`` ignored) across up to three ``conversations.list`` pages.
    Slack answers ``ok: false`` with ``users_not_found`` for an unknown email,
    which becomes ``{"found": False, "user": None}``; any other Slack error
    raises RuntimeError with the error code.
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
    return {"found": False, "channel": None}


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
