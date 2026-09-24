"""Slack connections authenticate with a directly supplied token.

Slack workspaces connect by pasting a bot (``xoxb-``), user (``xoxp-``), or
app-level (``xapp-``) token instead of an OAuth consent round-trip through
Dapier. The token is verified against Slack's ``auth.test`` before it is
stored, so a connected Slack connection always carries a checked identity.
Network access goes through an injectable ``transport`` callable, matching
``oauth_providers``.
"""

import json


SLACK_AUTH_TEST_URL = "https://slack.com/api/auth.test"
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
