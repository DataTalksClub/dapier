"""telegram_send action: post through a Telegram bot connection."""
from ...connections import credentials
from ...connections.providers import telegram_api
from . import base
from .templating import render


def run_telegram_send(action, event, *, transport=None, steps=None):
    """Post a message through a Telegram bot connection.

    The target chat defaults to the chat a telegram trigger fired from, so
    a trigger can reply in place; other triggers name the chat explicitly.
    """
    connection = base._connected_connection(action["connection_id"])
    secret = credentials.get_credential(connection["credential_id"])
    token = secret.get("token")
    if not token:
        raise ValueError("Telegram connection has no stored bot token")
    data = event.get("data", {})
    chat_id = action.get("chat_id") or data.get("chat_id")
    if chat_id is None or str(chat_id).strip() == "":
        raise ValueError("telegram_send needs a chat_id in the action or the triggering message")
    text = render(action.get("text", "{text}"), event, steps)
    result = telegram_api.send_message(
        token, chat_id, text, transport=transport,
        timeout=action.get("timeout_seconds", 10),
    )
    if not result:
        raise RuntimeError("Telegram did not confirm the message")
    return {
        "message_id": result.get("message_id"),
        "chat_id": (result.get("chat") or {}).get("id", chat_id),
    }


def run_telegram_find_chat(action, event, *, transport=None, steps=None):
    """Look up one chat's profile through the bot connection (getChat).

    A chat the bot cannot see is a verdict (``found: False``), not an error
    — Telegram rejects getChat with "chat not found" — so a workflow can
    branch on the outcome; every other Telegram rejection still raises.
    """
    connection = base._connected_connection(action["connection_id"])
    secret = credentials.get_credential(connection["credential_id"])
    token = secret.get("token")
    if not token:
        raise ValueError("Telegram connection has no stored bot token")
    chat_id = render(str(action.get("chat_id") or ""), event, steps).strip()
    if not chat_id:
        raise ValueError("telegram_find_chat requires a chat_id")
    try:
        chat = telegram_api.call(
            token, "getChat", {"chat_id": chat_id}, transport=transport) or {}
    except telegram_api.TelegramApiError as exc:
        if "chat not found" in str(exc).lower():
            return {"found": False, "chat": None}
        raise
    return {
        "found": True,
        "chat": {
            "id": chat.get("id"),
            "title": chat.get("title"),
            "username": chat.get("username"),
            "type": chat.get("type"),
        },
    }
