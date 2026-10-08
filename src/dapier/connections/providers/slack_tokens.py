"""Slack connections authenticate with a directly supplied token.

Slack workspaces connect by pasting a bot (``xoxb-``), user (``xoxp-``), or
app-level (``xapp-``) token instead of an OAuth consent round-trip through
Dapier. The token is verified against Slack's ``auth.test`` before it is
stored, so a connected Slack connection always carries a checked identity.
Network access goes through an injectable ``transport`` callable, matching
``oauth_providers``.
"""

import json
from urllib.parse import urlencode


SLACK_API_URL = "https://slack.com/api"
SLACK_AUTH_TEST_URL = f"{SLACK_API_URL}/auth.test"
TOKEN_PREFIXES = ("xoxb-", "xoxp-", "xapp-")


class SlackTokenError(Exception):
    """The token is malformed or Slack rejected it. Messages carry no secret."""


def validate_token(token):
    """Return the stripped token or raise SlackTokenError on a bad shape."""
    token = str(token or "").strip()
    if not token.startswith(TOKEN_PREFIXES) or len(token) < 20:
        raise SlackTokenError("Enter a valid Slack bot (xoxb-) or user (xoxp-) token")
    return token


def _default_transport(method, url, *, headers=None, body=None, timeout=10):
    import urllib.request

    request = urllib.request.Request(url, data=body, headers=headers or {}, method=method)
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.status, response.read()


def verify_account(token, *, transport=None):
    """Return ``(account_id, account_title)`` for the token's workspace identity.

    Fails closed: any network error, non-OK Slack response, or missing
    identity raises SlackTokenError instead of returning an unverified id.
    """
    transport = transport or _default_transport
    try:
        status, raw = transport(
            "POST", SLACK_AUTH_TEST_URL,
            headers={"authorization": f"Bearer {token}",
                     "content-type": "application/json"},
            body=b"{}", timeout=10,
        )
    except Exception as exc:
        raise SlackTokenError(f"Slack identity check unreachable: {type(exc).__name__}")
    try:
        data = json.loads(raw.decode() or "{}")
    except (ValueError, UnicodeDecodeError):
        raise SlackTokenError(f"Slack identity check returned HTTP {status}")
    if status >= 300 or not isinstance(data, dict) or not data.get("ok"):
        error = data.get("error", "unknown_error") if isinstance(data, dict) else "unreadable response"
        raise SlackTokenError(f"Slack rejected the token: {error}")
    account_id = data.get("team_id") or data.get("user_id")
    if not account_id:
        raise SlackTokenError("Slack identity check returned no account")
    return account_id, data.get("team") or data.get("user") or account_id


def token_kind(token):
    """Who a token acts as, from its prefix alone: ``app`` (bot or app-level
    token) or ``user`` (a person's token). None when the shape is unknown."""
    token = str(token or "")
    if token.startswith(("xoxb-", "xapp-")):
        return "app"
    if token.startswith("xoxp-"):
        return "user"
    return None


def _call(token, method, params, transport):
    """One Slack Web API call; the parsed body, or None on any failure."""
    try:
        status, raw = transport(
            "POST", f"{SLACK_API_URL}/{method}",
            headers={"authorization": f"Bearer {token}",
                     "content-type": "application/x-www-form-urlencoded"},
            body=urlencode(params or {}).encode(), timeout=10,
        )
        data = json.loads(raw.decode() or "{}")
    except Exception:
        return None
    if status >= 300 or not isinstance(data, dict) or not data.get("ok"):
        return None
    return data


def describe_token(token, *, transport=None):
    """Best effort: ``{"kind": "app"|"user", "name": ...}`` — who the token
    acts as inside its workspace: the app's name for a bot token
    (``bots.info``), the person's real name for a user token (``users.info``).

    A bot and a user token for one workspace share the workspace title; this
    is what tells the two connections apart. Never raises: when Slack cannot
    be asked, the kind still comes from the token prefix and the name falls
    back to the ``auth.test`` handle, else None.
    """
    transport = transport or _default_transport
    kind = token_kind(token)
    auth = _call(token, "auth.test", {}, transport) or {}
    name = None
    if auth.get("bot_id"):
        kind = "app"
        bot = (_call(token, "bots.info", {"bot": auth["bot_id"]}, transport) or {}).get("bot") or {}
        name = bot.get("name")
    elif auth.get("user_id") and kind != "app":
        kind = "user"
        user = (_call(token, "users.info", {"user": auth["user_id"]}, transport) or {}).get("user") or {}
        profile = user.get("profile") or {}
        name = user.get("real_name") or profile.get("real_name") or profile.get("display_name")
    name = name or auth.get("user") or None
    if not kind:
        return None
    return {"kind": kind, "name": name}


def identity_label(identity):
    """``Au-Tomator (App)`` / ``Alexey Grigorev (User)``; ``App token`` when
    only the kind is known; empty without an identity."""
    if not isinstance(identity, dict) or identity.get("kind") not in ("app", "user"):
        return ""
    kind = identity["kind"].capitalize()
    return f"{identity['name']} ({kind})" if identity.get("name") else f"{kind} token"
