"""Telegram connector: post messages through a bot connection (sends,
poll, edit-message), plus member/message maintenance (pin, ban, unban),
chat discovery and the getMe health check, both delegated to the shared
provider layer (``connections.discovery``) like the Sheets module.

The ``chats`` listing reads the bot's pending updates, which Telegram only
serves while no webhook is set — the same webhook Dapier's triggers use —
so live bots surface Telegram's own conflict message instead of a listing.
"""
from plugins.telegram.runners.telegram import (
    run_telegram_ban_member,
    run_telegram_edit_message,
    run_telegram_find_chat,
    run_telegram_pin_message,
    run_telegram_send,
    run_telegram_send_document,
    run_telegram_send_photo,
    run_telegram_send_poll,
    run_telegram_unban_member,
)
from src.dapier.connections import discovery as provider
from src.dapier.connectors import trigger_discovery
from src.dapier.connectors.registry import (
    Action,
    Connector,
    ConnectionTest,
    Discovery,
    connector,
    register,
    register_connection_test,
    register_discovery,
)
from src.dapier.connectors.trigger_discovery import (
    DEFAULT_LIMIT,
    DiscoveryNotFound,
    DiscoveryUpstream,
    TriggerDiscovery,
    per_event_sample_fetch,
    register_trigger_discovery,
)

connector(Connector(name="telegram", label="Telegram",
                    events=("message.received", "channel_post.received",
                            "callback_query.received"),
                    icon="send"))

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


register(Action(
    type="telegram_send_photo",
    label="Telegram send photo",
    icon="telegram",
    description=("Send a photo to a chat (Bot API sendPhoto). The media comes "
                 "from exactly one source — source_url (dapier downloads it, so "
                 "it need not be publicly reachable by Telegram) or source_s3 "
                 "{bucket, key} (a staged object, e.g. dropbox_read_file's "
                 "output). Output: {message_id, chat_id}."),
    run=lambda action, event, workflow_id, steps=None: run_telegram_send_photo(
        action, event, steps=steps),
    required=frozenset({"connection_id"}),
    optional=frozenset({"chat_id", "source_url", "source_s3", "filename",
                        "caption", "timeout_seconds"}),
    fields=(
        {"key": "connection_id", "label": "Connection ID", "required": True},
        {"key": "chat_id", "label": "Chat ID",
         "placeholder": "defaults to the triggering chat",
         "discover": {"resource": "telegram.chats"}},
        {"key": "source_url", "label": "Media URL", "type": "url",
         "placeholder": "https://example.test/report.png",
         "help": "One media source: a URL dapier downloads — or a staged "
                 "source_s3 {bucket, key} object. Bot API photos must be "
                 "JPEG/PNG/GIF/WEBP under 10 MB"},
        {"key": "filename", "label": "Filename override",
         "help": "Defaults to the URL's or object key's file name"},
        {"key": "caption", "label": "Caption", "type": "textarea",
         "placeholder": "New mail: {subject}"},
        {"key": "timeout_seconds", "label": "Timeout (s)", "type": "number"},
    ),
))

register(Action(
    type="telegram_send_document",
    label="Telegram send document",
    icon="telegram",
    description=("Send a file to a chat (Bot API sendDocument). The media comes "
                 "from exactly one source — source_url (dapier downloads it, so "
                 "it need not be publicly reachable by Telegram) or source_s3 "
                 "{bucket, key} (a staged object, e.g. dropbox_read_file's "
                 "output). Any file type works (up to 50 MB); the filename rides "
                 "along so the chat sees it. Output: {message_id, chat_id}."),
    run=lambda action, event, workflow_id, steps=None: run_telegram_send_document(
        action, event, steps=steps),
    required=frozenset({"connection_id"}),
    optional=frozenset({"chat_id", "source_url", "source_s3", "filename",
                        "caption", "timeout_seconds"}),
    fields=(
        {"key": "connection_id", "label": "Connection ID", "required": True},
        {"key": "chat_id", "label": "Chat ID",
         "placeholder": "defaults to the triggering chat",
         "discover": {"resource": "telegram.chats"}},
        {"key": "source_url", "label": "Media URL", "type": "url",
         "placeholder": "https://example.test/report.pdf",
         "help": "One media source: a URL dapier downloads — or a staged "
                 "source_s3 {bucket, key} object"},
        {"key": "filename", "label": "Filename override",
         "help": "Defaults to the URL's or object key's file name"},
        {"key": "caption", "label": "Caption", "type": "textarea",
         "placeholder": "New mail: {subject}"},
        {"key": "timeout_seconds", "label": "Timeout (s)", "type": "number"},
    ),
))

register(Action(
    type="telegram_send_poll",
    label="Telegram send poll",
    icon="telegram",
    description=("Send a poll to a chat (Bot API sendPoll). options holds one "
                 "option per line — 2 to 10 after trimming empty lines; "
                 "chat_id falls back to the triggering chat like the other "
                 "sends. Output: {message_id, chat_id, poll: {id, question}}."),
    run=lambda action, event, workflow_id, steps=None: run_telegram_send_poll(
        action, event, steps=steps),
    required=frozenset({"connection_id", "question", "options"}),
    optional=frozenset({"chat_id", "anonymous", "timeout_seconds"}),
    fields=(
        {"key": "connection_id", "label": "Connection ID", "required": True},
        {"key": "chat_id", "label": "Chat ID",
         "placeholder": "defaults to the triggering chat",
         "discover": {"resource": "telegram.chats"}},
        {"key": "question", "label": "Question", "type": "text", "required": True,
         "placeholder": "Ship on Friday?",
         "help": "Rendered from the event"},
        {"key": "options", "label": "Options", "type": "textarea", "required": True,
         "placeholder": "Yes\nNo\nNeeds another week",
         "help": "One option per line — 2 to 10 options, empty lines ignored"},
        {"key": "anonymous", "label": "Anonymous voting", "type": "boolean",
         "default": "true",
         "help": "Telegram hides who voted when on (the API's is_anonymous)"},
        {"key": "timeout_seconds", "label": "Timeout (s)", "type": "number"},
    ),
))


register(Action(
    type="telegram_edit_message",
    label="Telegram edit message",
    icon="telegram",
    description=("Edit one already-posted message's text (Bot API "
                 "editMessageText). chat_id falls back to the triggering "
                 "chat like the other telegram actions; message_id comes "
                 "from the trigger or an earlier step. Output: "
                 "{message_id, chat_id}."),
    run=lambda action, event, workflow_id, steps=None: run_telegram_edit_message(
        action, event, steps=steps),
    required=frozenset({"connection_id", "message_id", "text"}),
    optional=frozenset({"chat_id", "timeout_seconds"}),
    fields=(
        {"key": "connection_id", "label": "Connection ID", "required": True},
        {"key": "chat_id", "label": "Chat ID",
         "placeholder": "defaults to the triggering chat",
         "discover": {"resource": "telegram.chats"}},
        {"key": "message_id", "label": "Message ID", "required": True,
         "placeholder": "{message_id}",
         "help": "The message to edit — a telegram trigger envelope carries it"},
        {"key": "text", "label": "New text", "type": "textarea", "required": True,
         "placeholder": "{text} (edited by the workflow)"},
        {"key": "timeout_seconds", "label": "Timeout (s)", "type": "number"},
    ),
))


register(Action(
    type="telegram_pin_message",
    label="Telegram pin message",
    icon="telegram",
    description=("Pin one message in a chat (Bot API pinChatMessage). "
                 "chat_id falls back to the triggering chat; message_id "
                 "comes from the trigger or an earlier step; "
                 "disable_notification pins silently. Output: {pinned: true, "
                 "chat_id, message_id}."),
    run=lambda action, event, workflow_id, steps=None: run_telegram_pin_message(
        action, event, steps=steps),
    required=frozenset({"connection_id", "message_id"}),
    optional=frozenset({"chat_id", "disable_notification", "timeout_seconds"}),
    fields=(
        {"key": "connection_id", "label": "Connection ID", "required": True},
        {"key": "chat_id", "label": "Chat ID",
         "placeholder": "defaults to the triggering chat",
         "discover": {"resource": "telegram.chats"}},
        {"key": "message_id", "label": "Message ID", "required": True,
         "placeholder": "{message_id}",
         "help": "The message to pin — a telegram trigger envelope carries it"},
        {"key": "disable_notification", "label": "Pin silently", "type": "boolean",
         "default": "false",
         "help": "Telegram skips the pin notification when on"},
        {"key": "timeout_seconds", "label": "Timeout (s)", "type": "number"},
    ),
))


register(Action(
    type="telegram_ban_member",
    label="Telegram ban member",
    icon="telegram",
    description=("Ban one member from a chat (Bot API banChatMember). "
                 "chat_id falls back to the triggering chat; user_id renders "
                 "from the event — chain telegram_find_chat or pass "
                 "{user_id} from a membership trigger. Optional until_date "
                 "bans until an epoch timestamp (empty means forever). "
                 "Output: {banned: true, chat_id, user_id}."),
    run=lambda action, event, workflow_id, steps=None: run_telegram_ban_member(
        action, event, steps=steps),
    required=frozenset({"connection_id", "user_id"}),
    optional=frozenset({"chat_id", "until_date"}),
    fields=(
        {"key": "connection_id", "label": "Connection ID", "required": True},
        {"key": "chat_id", "label": "Chat ID",
         "placeholder": "defaults to the triggering chat",
         "discover": {"resource": "telegram.chats"}},
        {"key": "user_id", "label": "User ID", "required": True,
         "placeholder": "{user_id}",
         "help": "Rendered from the event — the member to ban"},
        {"key": "until_date", "label": "Banned until",
         "placeholder": "1798761600",
         "help": "Optional epoch seconds — empty bans forever"},
    ),
))


register(Action(
    type="telegram_unban_member",
    label="Telegram unban member",
    icon="telegram",
    description=("Unban one member of a chat (Bot API unbanChatMember). "
                 "chat_id falls back to the triggering chat; user_id renders "
                 "from the event. Output: {unbanned: true, chat_id, user_id}."),
    run=lambda action, event, workflow_id, steps=None: run_telegram_unban_member(
        action, event, steps=steps),
    required=frozenset({"connection_id", "user_id"}),
    optional=frozenset({"chat_id"}),
    fields=(
        {"key": "connection_id", "label": "Connection ID", "required": True},
        {"key": "chat_id", "label": "Chat ID",
         "placeholder": "defaults to the triggering chat",
         "discover": {"resource": "telegram.chats"}},
        {"key": "user_id", "label": "User ID", "required": True,
         "placeholder": "{user_id}",
         "help": "Rendered from the event — the member to unban"},
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

# Realistic updates, delivered-shaped through ``hook_triggers.update_data``
# (the same builder a real delivery uses), for the no-live tail of the
# sample chain — the designer preview must always render. One per telegram
# event: a direct message, a channel announcement (the Bot API's
# channel_post — chat with a title, an author signature instead of a
# visible "from"), and an inline-keyboard button tap (callback_query — the
# tapper, the button's payload string, and the message the button rode on).
_SYNTHETIC_UPDATE = {
    "update_id": 90125,
    "message": {
        "message_id": 7,
        "text": "Hello dapier",
        "chat": {"id": 555, "type": "private", "first_name": "Ada"},
        "from": {"id": 9, "is_bot": False, "first_name": "Ada", "language_code": "en"},
    },
}

_SYNTHETIC_CHANNEL_POST = {
    "update_id": 90126,
    "channel_post": {
        "message_id": 21,
        "text": "New cohort starts Monday — join through the pinned post",
        "chat": {"id": -1001730331343, "title": "DataTalksClub Courses",
                 "type": "channel"},
        "author_signature": "DataTalksClub",
    },
}

_SYNTHETIC_CALLBACK_QUERY = {
    "update_id": 90127,
    "callback_query": {
        "id": "4382bfdwdsb323b2d9",
        "from": {"id": 9, "is_bot": False, "first_name": "Ada",
                 "username": "ada", "language_code": "en"},
        "message": {
            "message_id": 27,
            "text": "Pick a cohort:",
            "chat": {"id": 555, "type": "private", "first_name": "Ada"},
            "reply_markup": {"inline_keyboard": [[
                {"text": "September", "callback_data": "join:september"},
                {"text": "October", "callback_data": "join:october"},
            ]]},
        },
        "chat_instance": "-9923423423",
        "data": "join:september",
    },
}


def _live_hook_id():
    """The enabled stored telegram hook's id, when one exists; else a marker.

    The sample's ``hook`` field matches what a real delivery would carry, so
    templates referencing ``{hook}`` preview truthfully.
    """
    from src.dapier.triggers import hook_triggers

    try:
        items = hook_triggers.load_items()
    except hook_triggers.TriggerError:
        return "discover"
    for item in items:
        if item.get("kind") == "telegram" and item.get("enabled", True):
            return item.get("hook_id") or "discover"
    return "discover"


def _fetch_sample(event=None, connection_id=None, limit=DEFAULT_LIMIT, transport=None):
    """One real Telegram update via ``getUpdates``, delivery-shaped, per event.

    The ``event`` request field picks which telegram event to sample —
    ``message.received`` (the default), ``channel_post.received`` or
    ``callback_query.received`` — and the live fetch picks the first pending
    update that actually arrives as that event. Read-only: updates are never confirmed (no offset), so the bot's
    own webhook keeps delivering them. A webhook-mode bot cannot also poll —
    Telegram rejects ``getUpdates`` — and that is the normal working state
    of a dapier telegram trigger, not an error: like the other chips, the
    chain is live, then the newest recorded run carrying the asked event,
    then a documented example per event, so the sample pull answers with
    something a workflow author can build on. Only a connection the operator
    named explicitly that is not connected still surfaces as a 502.
    """
    from src.dapier.connections.providers import telegram_api
    from src.dapier.triggers import hook_triggers

    try:
        connection = trigger_discovery.connected_connection("telegram", connection_id)
    except DiscoveryNotFound:
        connection = None  # nothing connected yet — fall through like slack/youtube
    if connection is not None:
        try:
            secret = trigger_discovery.stored_secret(
                connection["credential_id"], f"telegram connection '{connection['connection_id']}'")
            token = secret.get("token") or secret.get("bot_token")
            if not token:
                raise DiscoveryUpstream(
                    f"connection '{connection['connection_id']}' has no stored bot token")
            updates = telegram_api.call(token, "getUpdates", {"limit": 100},
                                        transport=transport) or []
            wanted = event or ""
            update = next((item for item in updates
                           if isinstance(item, dict)
                           and (not wanted
                                or hook_triggers.telegram_event_for(item) == wanted)),
                          None)
            if update is not None:
                hook_id = _live_hook_id()
                return {
                    "sample": trigger_discovery.as_sample({
                        "connector": "telegram",
                        "event": hook_triggers.telegram_event_for(update),
                        "source": hook_id,
                        "data": hook_triggers.update_data(update, hook_id),
                    }),
                    "source": "live",
                    "connection_id": connection["connection_id"],
                }
        except (DiscoveryNotFound, DiscoveryUpstream, telegram_api.TelegramApiError):
            pass  # webhook-mode bot, no pending updates, provider trouble — fall through
    hook_id = _live_hook_id()
    per_event = per_event_sample_fetch("telegram", hook_triggers.TELEGRAM_EVENT, {
        hook_triggers.TELEGRAM_EVENT:
            hook_triggers.update_data(_SYNTHETIC_UPDATE, hook_id),
        hook_triggers.TELEGRAM_CHANNEL_POST_EVENT:
            hook_triggers.update_data(_SYNTHETIC_CHANNEL_POST, hook_id),
        hook_triggers.TELEGRAM_CALLBACK_QUERY_EVENT:
            hook_triggers.update_data(_SYNTHETIC_CALLBACK_QUERY, hook_id),
    })
    return per_event(event=event, connection_id=connection_id, limit=limit)


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
