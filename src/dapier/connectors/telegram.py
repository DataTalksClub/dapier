"""Telegram connector: post messages through a bot connection, plus chat
discovery and the getMe health check, both delegated to the shared provider
layer (``connections.discovery``) like the Sheets module.

The ``chats`` listing reads the bot's pending updates, which Telegram only
serves while no webhook is set — the same webhook Dapier's triggers use —
so live bots surface Telegram's own conflict message instead of a listing.
"""
from ..connections import discovery as provider
from ..engine.actions.telegram import run_telegram_find_chat, run_telegram_send
from . import trigger_discovery
from .registry import (
    Action,
    ConnectionTest,
    Discovery,
    register,
    register_connection_test,
    register_discovery,
)
from .trigger_discovery import (
    DEFAULT_LIMIT,
    DiscoveryNotFound,
    DiscoveryUpstream,
    TriggerDiscovery,
    register_trigger_discovery,
)

register(Action(
    type="telegram_send",
    label="Telegram",
    icon="send",
    run=lambda action, event, workflow_id, steps=None: run_telegram_send(action, event, steps=steps),
    required=frozenset({"connection_id"}),
    optional=frozenset({"chat_id", "text", "timeout_seconds"}),
    fields=(
        {"key": "connection_id", "label": "Connection ID", "required": True},
        {"key": "chat_id", "label": "Chat ID", "placeholder": "defaults to the triggering chat",
         "discover": {"resource": "telegram.chats"}},
        {"key": "text", "label": "Text template", "type": "textarea", "placeholder": "{text}"},
        {"key": "timeout_seconds", "label": "Timeout (s)", "type": "number"},
    ),
))


register(Action(
    type="telegram_find_chat",
    label="Telegram find chat",
    description="Look up one chat's profile (getChat); found is False when the bot cannot see it",
    icon="telegram",
    run=lambda action, event, workflow_id, steps=None: run_telegram_find_chat(
        action, event, steps=steps),
    required=frozenset({"connection_id", "chat_id"}),
    optional=frozenset(),
    fields=(
        {"key": "connection_id", "label": "Connection ID", "required": True},
        {"key": "chat_id", "label": "Chat ID", "required": True,
         "placeholder": "@channel or -100…",
         "discover": {"resource": "telegram.chats"}},
    ),
))


def _run_chats(connection, params, *, transport=None):
    return provider.discover(connection, "chats", params, transport=transport)


register_discovery(Discovery(
    name="chats",
    connector="telegram",
    label="Chats",
    description="Chats the bot has received messages from recently",
    run=_run_chats,
))


def _run_test(connection, *, transport=None):
    return provider.test_connection(connection, transport=transport)


register_connection_test(ConnectionTest(connector="telegram", run=_run_test))


# --- trigger discovery: one live update as the sample event --------------------

def _live_hook_id():
    """The enabled stored telegram hook's id, when one exists; else a marker.

    The sample's ``hook`` field matches what a real delivery would carry, so
    templates referencing ``{hook}`` preview truthfully.
    """
    from ..triggers import hook_triggers

    try:
        items = hook_triggers.load_items()
    except hook_triggers.TriggerError:
        return "discover"
    for item in items:
        if item.get("kind") == "telegram" and item.get("enabled", True):
            return item.get("hook_id") or "discover"
    return "discover"


def _fetch_sample(event=None, connection_id=None, limit=DEFAULT_LIMIT, transport=None):
    """One real Telegram update via ``getUpdates``, delivery-shaped.

    Read-only: updates are never confirmed (no offset), so the bot's own
    webhook keeps delivering them. A webhook-mode bot cannot also poll —
    Telegram rejects ``getUpdates`` and discovery surfaces the provider's
    own message as a 502.
    """
    from ..connections.providers import telegram_api
    from ..triggers import hook_triggers

    connection = trigger_discovery.connected_connection("telegram", connection_id)
    secret = trigger_discovery.stored_secret(
        connection["credential_id"], f"telegram connection '{connection['connection_id']}'")
    token = secret.get("token") or secret.get("bot_token")
    if not token:
        raise DiscoveryUpstream(
            f"connection '{connection['connection_id']}' has no stored bot token")
    try:
        updates = telegram_api.call(token, "getUpdates", {"limit": 100}, transport=transport) or []
    except telegram_api.TelegramApiError as exc:
        raise DiscoveryUpstream(str(exc)) from exc
    update = next((item for item in updates if isinstance(item, dict)), None)
    if update is None:
        raise DiscoveryNotFound(
            "Telegram returned no pending updates; send the bot a message and retry")
    hook_id = _live_hook_id()
    return {
        "sample": trigger_discovery.as_sample({
            "connector": "telegram",
            "event": event or hook_triggers.TELEGRAM_EVENT,
            "source": hook_id,
            "data": hook_triggers.update_data(update, hook_id),
        }),
        "source": "live",
        "connection_id": connection["connection_id"],
    }


register_trigger_discovery(TriggerDiscovery(
    connector="telegram", label="Telegram", kind="sample", resource="",
    fetch=_fetch_sample))


# --- trigger discovery: chat options for the send/find actions' chat field ------

def _fetch_chat_options(event=None, connection_id=None, limit=DEFAULT_LIMIT):
    """Chat options via the registry chats listing (chats seen in recent
    updates). The value is the chat id — what telegram_send stores."""
    return trigger_discovery.options_from_registry(
        "telegram.chats", connection_id, limit, provider="telegram",
        option_of=lambda item: {"value": item.get("id"),
                                "label": item.get("name") or item.get("id")})


register_trigger_discovery(TriggerDiscovery(
    connector="telegram", label="Telegram", kind="options", resource="telegram.chats",
    fetch=_fetch_chat_options))
