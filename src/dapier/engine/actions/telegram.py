"""telegram_send action: post through a Telegram bot connection."""
from ...connections import credentials
from ...connections.providers import telegram_api
from . import base


def run_telegram_send(action, event, *, transport=None):
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
    template = action.get("text", "{text}")
    text = template.format_map(base._SafeFormat(data if isinstance(data, dict) else {}))
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
