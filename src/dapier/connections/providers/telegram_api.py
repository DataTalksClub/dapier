"""Telegram Bot API calls for triggers and send actions.

Telegram bots authenticate with a directly supplied token (``123456:ABC…``)
instead of an OAuth consent round-trip. Trigger registration uses
``setWebhook`` with a per-trigger ``secret_token`` that Telegram echoes back
on every delivery in ``X-Telegram-Bot-Api-Secret-Token``, so inbound updates
are verified without any shared infrastructure. Network access goes through
an injectable ``transport`` callable, matching ``slack_tokens`` and
``oauth_providers``.
"""

import json
import re

BASE_URL = "https://api.telegram.org"
TOKEN_PATTERN = re.compile(r"^\d{5,}:[A-Za-z0-9_-]{30,}$")


class TelegramApiError(Exception):
    """The token is malformed or Telegram rejected a call. Messages carry no secret."""


def validate_token(token):
    """Return the stripped token or raise TelegramApiError on a bad shape."""
    token = str(token or "").strip()
    if not TOKEN_PATTERN.fullmatch(token):
        raise TelegramApiError("Enter a valid Telegram bot token from @BotFather (digits:secret)")
    return token


def _default_transport(method, url, *, headers=None, body=None, timeout=10):
    import urllib.request

    request = urllib.request.Request(url, data=body, headers=headers or {}, method=method)
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.status, response.read()


def call(token, method, payload=None, *, transport=None, timeout=10):
    """Invoke one Bot API method and return its ``ok`` result.

    Fails closed: any network error, non-2xx response, or ``ok: false``
    result raises TelegramApiError instead of returning a partial value.
    """
    transport = transport or _default_transport
    body = json.dumps(payload or {}).encode()
    import urllib.error

    try:
        status, raw = transport(
            "POST", f"{BASE_URL}/bot{token}/{method}",
            headers={"content-type": "application/json"},
            body=body, timeout=timeout,
        )
    except urllib.error.HTTPError as exc:
        # A non-2xx is still Telegram answering: the error body carries the
        # real rejection (rate limit, bad secret token), so surface it
        # instead of masking it as a network problem.
        try:
            data = json.loads(exc.read().decode() or "{}")
            description = data.get("description") if isinstance(data, dict) else None
        except (ValueError, UnicodeDecodeError, OSError):
            description = None
        raise TelegramApiError(
            f"Telegram rejected {method}: {description or f'HTTP {exc.code}'}"
        ) from exc
    except Exception as exc:
        raise TelegramApiError(f"Telegram API unreachable: {type(exc).__name__}")
    try:
        data = json.loads(raw.decode() or "{}")
    except (ValueError, UnicodeDecodeError):
        raise TelegramApiError(f"Telegram API returned HTTP {status}")
    if status >= 300 or not isinstance(data, dict) or not data.get("ok"):
        description = data.get("description", "unknown_error") if isinstance(data, dict) else "unreadable response"
        raise TelegramApiError(f"Telegram rejected {method}: {description}")
    return data.get("result")


def get_me(token, *, transport=None):
    """Return ``(bot_id, title)`` for the token's bot identity."""
    result = call(token, "getMe", transport=transport)
    bot_id = (result or {}).get("id")
    if not bot_id:
        raise TelegramApiError("Telegram identity check returned no bot id")
    username = (result or {}).get("username") or str(bot_id)
    return bot_id, f"@{username}"


def set_webhook(token, url, secret_token, *, transport=None):
    """Point the bot's (single) delivery URL at Dapier with a shared secret.

    Channel posts are included so a bot sitting in announcement channels can
    drive workflows (the au-tomator port); private/group messages stay
    subscribed for trigger-and-reply workflows.
    """
    call(token, "setWebhook", {
        "url": url,
        "secret_token": secret_token,
        "allowed_updates": ["message", "edited_message", "channel_post", "edited_channel_post"],
    }, transport=transport)


def delete_webhook(token, *, transport=None):
    call(token, "deleteWebhook", {"drop_pending_updates": False}, transport=transport)


def send_message(token, chat_id, text, *, transport=None, timeout=10):
    return call(token, "sendMessage", {
        "chat_id": chat_id,
        "text": text,
        "link_preview_options": {"is_disabled": True},
    }, transport=transport, timeout=timeout)
