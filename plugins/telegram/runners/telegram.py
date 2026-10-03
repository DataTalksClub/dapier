"""telegram_send / send_photo / send_document / send_poll / find_chat
actions: post through a Telegram bot connection — plus edit_message,
pin_message, ban_member and unban_member maintenance."""
import json
import mimetypes
import urllib.parse

from src.dapier.connections import credentials
from src.dapier.connections.providers import telegram_api
from src.dapier.engine.actions import base
from src.dapier.engine.actions.templating import render

MEDIA_DOWNLOAD_TIMEOUT = 30
MEDIA_UPLOAD_TIMEOUT = 30

# Multipart delimiter for the media methods (sent in the request's
# content-type header). Long and prefixed so uploaded bytes never contain it
# by accident.
MEDIA_BOUNDARY = "dapier-telegram-media-7f3d9c2"


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


# --- send_photo / send_document: media on the Bot API, multipart-uploaded ----


def _media_payload(action, event, steps, *, transport=None):
    """The media bytes plus a filename and a content type, from exactly one
    source: ``source_url`` (an HTTP download) or ``source_s3`` ``{bucket,
    key}`` (a staged object, like s3_upload's composition).

    The download runs from dapier, so ``source_url`` need not be publicly
    reachable by Telegram — any URL the run can fetch works, unlike passing
    a URL string to the Bot API directly. ``source_s3`` bucket/key take
    templates, so an earlier step's staged file (dropbox_read_file's or a
    render output) sends without a literal bucket/key in the workflow.
    """
    transport = transport or base._default_transport
    url = render(str(action.get("source_url") or ""), event, steps).strip()
    source = action.get("source_s3") if isinstance(action.get("source_s3"), dict) else {}
    staged = {
        "bucket": render(str(source.get("bucket") or ""), event, steps).strip(),
        "key": render(str(source.get("key") or ""), event, steps).strip(),
    }
    given = [name for name, value in (
        ("source_url", url),
        ("source_s3", staged["bucket"] and staged["key"]),
    ) if value]
    if len(given) > 1:
        raise ValueError("takes one media source "
                         f"({', '.join(given)} given): source_url or source_s3")
    if url:
        try:
            status, body = transport("GET", url, headers={}, body=None,
                                     timeout=MEDIA_DOWNLOAD_TIMEOUT)
        except Exception as exc:
            raise RuntimeError(f"media download unreachable: {type(exc).__name__}") from None
        if status >= 300:
            raise RuntimeError(f"media download returned HTTP {status}")
        default_name = urllib.parse.unquote(
            urllib.parse.urlparse(url).path.rsplit("/", 1)[-1]) or "file"
    elif staged["bucket"] and staged["key"]:
        body = base._s3_body(staged)
        default_name = staged["key"].rsplit("/", 1)[-1] or "file"
    else:
        raise ValueError("needs source_url or source_s3 with bucket and key")
    override = render(str(action.get("filename") or ""), event, steps).strip()
    filename = base._safe_filename(override or default_name).replace('"', "")
    content_type = mimetypes.guess_type(filename)[0] or "application/octet-stream"
    return body, filename, content_type


def _media_multipart(fields, file_field, filename, content_type, payload, boundary):
    """One multipart/form-data body: the text fields, then the media part."""
    parts = []
    for key, value in fields.items():
        parts.append(
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="{key}"\r\n\r\n'
            f"{value}\r\n".encode()
        )
    parts.append(
        (f"--{boundary}\r\n"
         f'Content-Disposition: form-data; name="{file_field}"; '
         f'filename="{filename}"\r\n'
         f"Content-Type: {content_type}\r\n\r\n").encode()
        + payload + b"\r\n"
    )
    parts.append(f"--{boundary}--\r\n".encode())
    return b"".join(parts)


def _media_call(token, method, body, content_type, *, transport=None, timeout=30):
    """Invoke one media Bot API method and return its ``ok`` result, with
    ``telegram_api.call``'s fail-closed semantics (a non-2xx is still
    Telegram answering, so its description surfaces)."""
    import urllib.error

    transport = transport or base._default_transport
    try:
        status, raw = transport(
            "POST", f"{telegram_api.BASE_URL}/bot{token}/{method}",
            headers={"content-type": content_type},
            body=body, timeout=timeout,
        )
    except urllib.error.HTTPError as exc:
        try:
            data = json.loads(exc.read().decode() or "{}")
            description = data.get("description") if isinstance(data, dict) else None
        except (ValueError, UnicodeDecodeError, OSError):
            description = None
        raise telegram_api.TelegramApiError(
            f"Telegram rejected {method}: {description or f'HTTP {exc.code}'}") from exc
    except Exception as exc:
        raise telegram_api.TelegramApiError(
            f"Telegram API unreachable: {type(exc).__name__}")
    try:
        data = json.loads(raw.decode() or "{}")
    except (ValueError, UnicodeDecodeError):
        raise telegram_api.TelegramApiError(f"Telegram API returned HTTP {status}")
    if status >= 300 or not isinstance(data, dict) or not data.get("ok"):
        description = (data.get("description", "unknown_error")
                       if isinstance(data, dict) else "unreadable response")
        raise telegram_api.TelegramApiError(f"Telegram rejected {method}: {description}")
    return data.get("result")


def _run_telegram_media(action, event, *, method, field, steps=None, transport=None):
    """The shared sendPhoto/sendDocument runner.

    ``chat_id`` behaves like :func:`run_telegram_send`: the stored value
    wins, otherwise the chat a telegram trigger fired from is answered.
    """
    connection = base._connected_connection(action["connection_id"])
    secret = credentials.get_credential(connection["credential_id"])
    token = secret.get("token")
    if not token:
        raise ValueError("Telegram connection has no stored bot token")
    data = event.get("data", {})
    chat_id = action.get("chat_id") or data.get("chat_id")
    if isinstance(chat_id, str):
        chat_id = render(chat_id, event, steps).strip()
    if chat_id is None or str(chat_id).strip() == "":
        raise ValueError(f"{action.get('type')} needs a chat_id in the action "
                         "or the triggering message")
    payload, filename, content_type = _media_payload(
        action, event, steps, transport=transport)
    fields = {"chat_id": str(chat_id)}
    caption = render(str(action.get("caption") or ""), event, steps).strip()
    if caption:
        fields["caption"] = caption
    body = _media_multipart(fields, field, filename, content_type, payload,
                            MEDIA_BOUNDARY)
    result = _media_call(
        token, method, body, f"multipart/form-data; boundary={MEDIA_BOUNDARY}",
        transport=transport,
        timeout=action.get("timeout_seconds", MEDIA_UPLOAD_TIMEOUT),
    )
    if not result:
        raise RuntimeError("Telegram did not confirm the media message")
    return {
        "message_id": result.get("message_id"),
        "chat_id": (result.get("chat") or {}).get("id", chat_id),
    }


def run_telegram_send_photo(action, event, *, transport=None, steps=None):
    """Send a photo to a chat through the bot connection (sendPhoto).

    The bytes come from exactly one source — ``source_url`` (dapier
    downloads it, so it need not be publicly reachable by Telegram) or a
    staged ``source_s3`` ``{bucket, key}`` object — and upload as
    multipart/form-data; the optional ``caption`` renders like a message.
    Bot API photos must be JPEG/PNG/GIF/WEBP under 10 MB. Output:
    ``{message_id, chat_id}``.
    """
    return _run_telegram_media(action, event, method="sendPhoto", field="photo",
                               steps=steps, transport=transport)


def run_telegram_send_document(action, event, *, transport=None, steps=None):
    """Send a file to a chat through the bot connection (sendDocument).

    Same media sourcing as :func:`run_telegram_send_photo`; any file type
    works (Bot API documents up to 50 MB) and the filename rides along so
    the chat sees it. Output: ``{message_id, chat_id}``.
    """
    return _run_telegram_media(action, event, method="sendDocument", field="document",
                               steps=steps, transport=transport)


# --- send_poll: a poll on the Bot API, plain JSON -------------------------------


def _bool_flag(value, default):
    """A tolerant boolean: stored form values may arrive as real booleans or
    the strings "true"/"false"; empty falls back to ``default``."""
    text = "" if value is None else str(value).strip()
    if not text:
        return default
    return text.lower() in ("true", "1", "yes", "on")


def _poll_options(raw, event, steps):
    """The poll's options: one per line in the textarea — split on newlines,
    render each line, trim empties. Telegram takes 2-10 options."""
    options = []
    for line in str(raw or "").splitlines():
        option = render(line, event, steps).strip()
        if option:
            options.append(option)
    if not 2 <= len(options) <= 10:
        raise ValueError(
            f"telegram_send_poll needs 2-10 options (one per line, empty "
            f"lines ignored), got {len(options)}")
    return options


def run_telegram_send_poll(action, event, *, transport=None, steps=None):
    """Send a poll to a chat through the bot connection (sendPoll, a plain
    JSON POST unlike the multipart media sends).

    ``chat_id`` falls back to the triggering chat like the other sends;
    ``question`` renders from the event; ``options`` holds one option per
    line (2-10 after trimming empty lines); ``anonymous`` maps to the API's
    ``is_anonymous`` and defaults to true. Output: ``{message_id, chat_id,
    poll: {id, question}}``, following telegram_send_photo's output style.
    """
    connection = base._connected_connection(action["connection_id"])
    secret = credentials.get_credential(connection["credential_id"])
    token = secret.get("token")
    if not token:
        raise ValueError("Telegram connection has no stored bot token")
    data = event.get("data", {})
    chat_id = action.get("chat_id") or data.get("chat_id")
    if isinstance(chat_id, str):
        chat_id = render(chat_id, event, steps).strip()
    if chat_id is None or str(chat_id).strip() == "":
        raise ValueError("telegram_send_poll needs a chat_id in the action or "
                         "the triggering message")
    question = render(str(action.get("question") or ""), event, steps).strip()
    if not question:
        raise ValueError("telegram_send_poll requires a question")
    payload = {
        "chat_id": chat_id,
        "question": question,
        "options": _poll_options(action.get("options"), event, steps),
        "is_anonymous": _bool_flag(action.get("anonymous"), True),
    }
    result = telegram_api.call(token, "sendPoll", payload, transport=transport,
                               timeout=action.get("timeout_seconds", 10))
    if not result:
        raise RuntimeError("Telegram did not confirm the poll")
    poll = result.get("poll") or {}
    return {
        "message_id": result.get("message_id"),
        "chat_id": (result.get("chat") or {}).get("id", chat_id),
        "poll": {"id": poll.get("id"), "question": poll.get("question")},
    }


# --- edit / pin / ban / unban: message and member maintenance -------------------


def _connection_token(action):
    """The bot token behind the action's ``connection_id``."""
    connection = base._connected_connection(action["connection_id"])
    secret = credentials.get_credential(connection["credential_id"])
    token = secret.get("token")
    if not token:
        raise ValueError("Telegram connection has no stored bot token")
    return token


def _resolve_chat_id(action, event, steps):
    """The target chat: the stored value wins, otherwise the chat a telegram
    trigger fired from is answered (run_telegram_send's rule)."""
    data = event.get("data", {})
    chat_id = action.get("chat_id") or data.get("chat_id")
    if isinstance(chat_id, str):
        chat_id = render(chat_id, event, steps).strip()
    if chat_id is None or str(chat_id).strip() == "":
        raise ValueError(f"{action.get('type')} needs a chat_id in the action "
                         "or the triggering message")
    return chat_id


def _api_int(raw):
    """A rendered value as the integer Telegram's Integer-typed fields want;
    a bare number converts, anything else passes through as the string."""
    text = str(raw or "").strip()
    return int(text) if text.lstrip("-").isdigit() else text


def run_telegram_edit_message(action, event, *, transport=None, steps=None):
    """Edit one already-posted message's text (editMessageText).

    ``chat_id`` falls back to the triggering chat like the other telegram
    actions; ``message_id`` names the message (a telegram trigger envelope
    carries it) and ``text`` replaces its body. Output:
    ``{message_id, chat_id}``.
    """
    token = _connection_token(action)
    chat_id = _resolve_chat_id(action, event, steps)
    message_id = render(str(action.get("message_id") or ""), event, steps).strip()
    if not message_id:
        raise ValueError("telegram_edit_message requires a message_id — a "
                         "telegram trigger envelope carries {message_id}")
    text = render(action.get("text", ""), event, steps)
    result = telegram_api.call(token, "editMessageText",
                               {"chat_id": chat_id, "message_id": _api_int(message_id),
                                "text": text},
                               transport=transport,
                               timeout=action.get("timeout_seconds", 10))
    if not result:
        raise RuntimeError("Telegram did not confirm the edit")
    return {
        "message_id": result.get("message_id") or _api_int(message_id),
        "chat_id": (result.get("chat") or {}).get("id", chat_id),
    }


def _run_telegram_member_action(action, event, *, method, past_tense,
                                extra_fields=(), steps=None, transport=None):
    """The shared ban/unban runner: one Bot API member call per chat.

    ``chat_id`` falls back to the triggering chat like the sends;
    ``user_id`` renders from the event (chain telegram_find_chat or pass
    ``{user_id}`` from a membership trigger); ``extra_fields`` names the
    optional keys (``until_date`` for banChatMember) that ride along when
    set. Telegram answers ``true`` for both; the output verdicts with
    ``{<past_tense>: True, chat_id, user_id}``.
    """
    token = _connection_token(action)
    chat_id = _resolve_chat_id(action, event, steps)
    user_id = render(str(action.get("user_id") or ""), event, steps).strip()
    if not user_id:
        raise ValueError(f"{action.get('type')} requires a user_id")
    payload = {"chat_id": chat_id, "user_id": _api_int(user_id)}
    for field in extra_fields:
        value = render(str(action.get(field) or ""), event, steps).strip()
        if value:
            payload[field] = _api_int(value)
    result = telegram_api.call(token, method, payload, transport=transport,
                               timeout=action.get("timeout_seconds", 10))
    if not result:
        raise RuntimeError(f"Telegram did not confirm the {method}")
    return {past_tense: True, "chat_id": chat_id, "user_id": _api_int(user_id)}


def run_telegram_ban_member(action, event, *, transport=None, steps=None):
    """Ban one member from a chat (banChatMember).

    Optional ``until_date`` bans until an epoch timestamp (missing or empty
    means forever). Output: ``{banned: True, chat_id, user_id}``.
    """
    return _run_telegram_member_action(
        action, event, method="banChatMember", past_tense="banned",
        extra_fields=("until_date",), steps=steps, transport=transport)


def run_telegram_unban_member(action, event, *, transport=None, steps=None):
    """Unban one member of a chat (unbanChatMember). Output:
    ``{unbanned: True, chat_id, user_id}``."""
    return _run_telegram_member_action(
        action, event, method="unbanChatMember", past_tense="unbanned",
        steps=steps, transport=transport)


def run_telegram_pin_message(action, event, *, transport=None, steps=None):
    """Pin one message in a chat (pinChatMessage).

    ``chat_id`` falls back to the triggering chat; ``message_id`` names the
    message to pin (a telegram trigger envelope carries it); optional
    ``disable_notification`` pins silently. Output: ``{pinned: True,
    chat_id, message_id}``.
    """
    token = _connection_token(action)
    chat_id = _resolve_chat_id(action, event, steps)
    message_id = render(str(action.get("message_id") or ""), event, steps).strip()
    if not message_id:
        raise ValueError("telegram_pin_message requires a message_id — a "
                         "telegram trigger envelope carries {message_id}")
    payload = {"chat_id": chat_id, "message_id": _api_int(message_id),
               "disable_notification": _bool_flag(
                   action.get("disable_notification"), False)}
    result = telegram_api.call(token, "pinChatMessage", payload,
                               transport=transport,
                               timeout=action.get("timeout_seconds", 10))
    if not result:
        raise RuntimeError("Telegram did not confirm the pin")
    return {"pinned": True, "chat_id": chat_id,
            "message_id": _api_int(message_id)}
