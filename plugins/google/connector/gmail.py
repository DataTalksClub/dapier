"""Gmail connector: read and send the connection's Gmail over a Google
connection — the ``gmail.messages`` poll source (new email on the poll
schedule), the ``gmail_send`` action, the labels listing behind the trigger
query's label picker, and the chip's sample pull.

The chip rides the shared Google OAuth provider like Sheets, Drive and
Calendar: one connection backs all four, so the provider's ``google``
connection test already answers for Gmail connections and none is
registered here. The scopes the chip needs beyond the identity check, kept
minimal and granted to the connection at connect time (like every Google
scope — the console's Google card or ``dapier connections create/edit
--scopes``; an existing connection gains them by updating its scopes and
reconnecting):

    https://www.googleapis.com/auth/gmail.readonly   (messages, labels)
    https://www.googleapis.com/auth/gmail.send       (the send action)

Those two lines are ``GMAIL_SCOPES`` in
``connections.providers.oauth_providers`` — the single declaration the
OAuth flow derives a Gmail connection's grant from (flow-owned glue, so it
lives in core). The ``GMAIL_SCOPES`` environment variable extends it with
live-use extras (gmail.modify, gmail.settings, gmail.labels) at consent
and verification time; see ``effective_gmail_scopes`` there.

The poll source keys on each message's ``internalDate`` — the drive files
source's watermark applied to mail: the first fire seeds the watermark at
now and emits nothing (the mailbox is history, not news), later fires emit
messages delivered strictly after it, oldest first, and the seen store
dedupes the re-listed ids.
"""
import base64
import urllib.parse
from datetime import datetime, timezone

from src.dapier.connections import discovery as provider
from plugins.google.runners.gmail import run_gmail_send
from src.dapier.triggers.poll_sources import PollSource, register_source
from src.dapier.connectors.registry import (
    Action,
    Connector,
    Discovery,
    connector,
    register,
    register_discovery,
)
from src.dapier.connectors import trigger_discovery
from src.dapier.connectors.trigger_discovery import (
    DEFAULT_LIMIT,
    TriggerDiscovery,
    options_from_registry,
    register_trigger_discovery,
)

GMAIL_API_URL = "https://gmail.googleapis.com/gmail/v1"
GMAIL_MESSAGES_URL = GMAIL_API_URL + "/users/me/messages"


# --- action: send an email from the connection's mailbox ----------------------

register(Action(
    type="gmail_send",
    label="Gmail",
    icon="mail",
    description=("Send an email from the connection's Gmail mailbox "
                 "(users.messages.send). Gmail delivers only from the "
                 "authenticated account, so there is no sender field. "
                 "Output: {message_id, thread_id, from, to, subject} plus "
                 "cc/bcc when set."),
    run=lambda action, event, workflow_id, steps=None: run_gmail_send(
        action, event, steps=steps),
    required=frozenset({"connection_id", "to"}),
    optional=frozenset({"subject", "text", "html", "cc", "bcc"}),
    fields=(
        {"key": "connection_id", "label": "Connection ID", "placeholder": "google",
         "required": True},
        {"key": "to", "label": "To", "type": "email", "required": True,
         "placeholder": "you@example.com or {sender}"},
        {"key": "subject", "label": "Subject", "placeholder": "{subject}"},
        {"key": "text", "label": "Text body", "type": "textarea"},
        {"key": "html", "label": "HTML body", "type": "textarea"},
        {"key": "cc", "label": "Cc", "type": "email",
         "placeholder": "copy@example.com, other@example.com"},
        {"key": "bcc", "label": "Bcc", "type": "email",
         "placeholder": "hidden@example.com"},
    ),
))


# --- trigger discovery: label options for the poll trigger's query field ------

def _run_labels(connection, params, *, transport=None):
    return provider.discover(connection, "labels", params, transport=transport)


register_discovery(Discovery(
    name="labels",
    connector="gmail",
    label="Gmail labels",
    description="Labels (mailboxes) in the connection's Gmail, system and user",
    run=_run_labels,
))


def _fetch_label_options(event=None, connection_id=None, limit=DEFAULT_LIMIT):
    """Gmail label options via the registry listing (first connected Google
    connection when no id is named). The value is the ``label:<name>`` term
    the poll trigger's query field takes verbatim."""
    return options_from_registry(
        "gmail.labels", connection_id, limit, provider="google",
        option_of=lambda item: {"value": f"label:{item.get('name') or item.get('id')}",
                                "label": item.get("name") or item.get("id")})


register_trigger_discovery(TriggerDiscovery(
    connector="gmail", label="Gmail", kind="options",
    resource="gmail.labels",
    fetch=_fetch_label_options))


# --- poll source: "New Email" on the poll-trigger schedule --------------------
#
# messages.list is JSON, so the generic poll trigger could read it — but a
# listing carries no timestamp to watermark: the stubs are bare id/threadId
# pairs, and the delivery time (internalDate) only rides the per-message
# resource. The ``gmail.messages`` source owns the transform — list the
# stubs, expand the page's messages one GET each, watermark the
# internalDate like the drive files source watermarks createdTime — and
# publishes ``gmail``/``message.received`` so workflows match the palette
# chip while staying scoped through the poll-name filter.

GMAIL_POLL_PAGE_SIZE = 50
GMAIL_POLL_PAGES = 2  # ~100 message stubs per fire

# Older than any internalDate (epoch milliseconds): the discovery sample's
# "show the mailbox's newest mail" fetch runs against this watermark.
GMAIL_EPOCH_CURSOR = "0"

# The body excerpt's cap: a text/plain part decoded for the event data is
# truncated here, so a pasted novel cannot bloat one fire's payloads.
GMAIL_EXCERPT_CHARS = 2000


def _gmail_now():
    """Gmail-format UTC now: internalDate renders epoch milliseconds, so the
    seed watermark compares against message stamps as an integer."""
    return str(int(datetime.now(timezone.utc).timestamp() * 1000))


def _gmail_poll_validate(body):
    """Save-time fetch spec: the ``connection_id`` of the Google connection
    to poll as (required — the fetch refreshes its OAuth token) and the
    optional Gmail ``query`` (a ``label:`` term from the labels listing, or
    any search expression — a whole-mailbox watch stays the default), plus
    the fetch defaults every stored gmail poll carries."""
    from src.dapier.triggers.email_triggers import TriggerError

    body = body if isinstance(body, dict) else {}
    if not str(body.get("connection_id") or "").strip():
        raise TriggerError(
            "connection_id is required: pick the Google connection to poll as")
    return {
        "query": str(body.get("query") or "").strip(),
        "cursor_mode": "next_cursor",
        "id_path": "id",
        "url": "",
    }


def _list_message_stubs(token, query, *, transport=None):
    """The mailbox's newest message stubs (id + threadId), up to ~100 across
    two pages of 50, with an optional ``q`` narrowing. messages.list returns
    ids only — the cursor filters on each message's internalDate, which only
    the per-message resource carries — so the stubs are expanded one GET
    each in :func:`_gmail_poll_fetch`."""
    page_params = {"maxResults": GMAIL_POLL_PAGE_SIZE}
    if query:
        page_params["q"] = query
    stubs = []
    for _page in range(GMAIL_POLL_PAGES):
        url = GMAIL_MESSAGES_URL + "?" + urllib.parse.urlencode(page_params)
        data = provider._request("GET", url, token, None, transport=transport)
        page = [entry for entry in data.get("messages") or []
                if isinstance(entry, dict) and entry.get("id")]
        stubs.extend(page)
        if not page or len(stubs) >= GMAIL_POLL_PAGE_SIZE * GMAIL_POLL_PAGES \
                or not data.get("nextPageToken"):
            return stubs
        page_params = {**page_params, "pageToken": data["nextPageToken"]}
    return stubs


def _body_excerpt(payload):
    """The first text/plain part's decoded text, capped at
    GMAIL_EXCERPT_CHARS. Gmail stores bodies base64url-encoded (unpadded)
    inside the payload tree; a message without a plain-text part — or an
    undecodable one — yields None and the snippet stands in."""
    stack = [payload] if isinstance(payload, dict) else []
    while stack:
        part = stack.pop(0)
        body = part.get("body") if isinstance(part.get("body"), dict) else {}
        if str(part.get("mimeType") or "") == "text/plain" and body.get("data"):
            data = str(body["data"])
            try:
                return base64.urlsafe_b64decode(
                    data + "=" * (-len(data) % 4)).decode("utf-8", "replace")[:GMAIL_EXCERPT_CHARS]
            except (ValueError, UnicodeDecodeError):
                return None
        stack.extend(part.get("parts") or [])
    return None


def _message_item(token, stub, *, transport=None):
    """One stub expanded to the event data shape: the addressing headers,
    the internal timestamp the cursor keys on, Gmail's own snippet, and the
    plain-text body excerpt."""
    quoted = urllib.parse.quote(str(stub["id"]), safe="")
    url = f"{GMAIL_MESSAGES_URL}/{quoted}?format=full"
    data = provider._request("GET", url, token, None, transport=transport)
    payload = data.get("payload") if isinstance(data.get("payload"), dict) else {}
    headers = {str(header.get("name") or "").lower(): str(header.get("value") or "")
               for header in payload.get("headers") or []
               if isinstance(header, dict) and header.get("name")}
    return {
        "id": data.get("id") or stub["id"],
        "thread_id": data.get("threadId"),
        "from": headers.get("from"),
        "to": headers.get("to"),
        "cc": headers.get("cc"),
        "subject": headers.get("subject"),
        "date": headers.get("date"),
        "internal_date": str(data.get("internalDate") or ""),
        "snippet": data.get("snippet"),
        "text": _body_excerpt(payload),
    }


def _internal_date(message):
    """The message's delivery stamp as an int (0 when the API omitted it —
    an unstamped message can never fire, it has no place in the order)."""
    try:
        return int(str(message.get("internal_date") or "").strip() or "0")
    except ValueError:
        return 0


def _gmail_poll_fetch(item, cursor=None, *, transport=None):
    """One poll page as ``(items, next_cursor)`` for ``triggers.poll_sources``.

    Lists the mailbox through Gmail's messages.list (the provider's shared
    request path), with the bearer token from
    ``poll_triggers._bearer_token`` so the connection's OAuth token is
    refreshed exactly like the classic fetch, then expands the page's
    stubs to the event data shape (one messages.get per stub).

    The cursor is an ``internalDate`` watermark in epoch milliseconds — the
    API renders plain digit strings, so they compare as integers. With no
    stored cursor — the first fire after enabling — the fetch only seeds the
    watermark at now and emits nothing: the mailbox's existing mail is
    history, not news (the page's stubs are not even expanded). With a
    cursor, only messages delivered strictly after it fire, oldest first,
    and the parked cursor is the newest fired internalDate (the incoming
    one when nothing qualifies; ``fire`` parks it only once the page
    drains). The ~100-stub page budget bounds one fire: mail beyond it
    waits for a quieter mailbox, the same polling caveat the sheets rows
    source carries. Raises ``RuntimeError`` on a failed fetch, like every
    poll source.
    """
    from src.dapier.triggers import poll_triggers

    if not str(item.get("connection_id") or "").strip():
        raise RuntimeError("poll source 'gmail.messages' needs connection_id: "
                           "the Google connection to poll as")
    token = poll_triggers._bearer_token(item["connection_id"])
    try:
        stubs = _list_message_stubs(token, str(item.get("query") or "").strip(),
                                    transport=transport)
        if cursor is None:
            # First fire: seed the watermark at now (the mailbox's existing
            # mail predates it) without emitting anything — and without
            # paying a messages.get per existing stub.
            return [], _gmail_now()
        messages = [_message_item(token, stub, transport=transport)
                    for stub in stubs]
    except provider.DiscoveryError as exc:
        raise RuntimeError(f"gmail poll failed: {exc}") from None
    watermark = _parse_cursor(cursor)
    fresh = sorted(
        (message for message in messages if _internal_date(message) > watermark),
        key=lambda message: (_internal_date(message), str(message.get("id") or "")))
    return fresh, str(fresh[-1]["internal_date"]) if fresh else str(cursor)


def _parse_cursor(cursor):
    """The stored watermark as an int; a foreign cursor reads as 0, which
    re-fires nothing older than epoch but never masks fresh mail."""
    try:
        return int(str(cursor).strip() or "0")
    except ValueError:
        return 0


def _gmail_poll_view(item):
    """The provider params ``public_view`` shows beside ``source``."""
    return {"query": item.get("query") or None}


register_source(PollSource(
    name="gmail.messages", connector="gmail", event="message.received",
    label="Gmail", validate=_gmail_poll_validate,
    fetch=_gmail_poll_fetch, view=_gmail_poll_view))


# --- the chip ----------------------------------------------------------------
#
# The palette chip registers from this module (import = registration) next
# to the poll source that fires its event — like every registration here,
# the connector registry is global.

connector(Connector(name="gmail", label="Gmail",
                    events=("message.received",), icon="mail",
                    event_info={"message.received": ("Email arrives", "A new message lands in the watched Gmail inbox or label")}))


# --- trigger discovery: the chip's sample pull --------------------------------

_GMAIL_SYNTHETIC_MESSAGE = {
    "id": "18c1a2b3c4d5e6f7",
    "thread_id": "18c1a2b3c4d5e6f7",
    "from": "Acme Billing <billing@example.test>",
    "to": "todo@dtcdev.click",
    "cc": None,
    "subject": "Invoice #4137 - September",
    "date": "Mon, 28 Sep 2026 09:14:03 +0000",
    "internal_date": "1790584443000",
    "snippet": "Invoice #4137 for September is attached.",
    "text": "Invoice #4137 for September is attached.",
}


def _stored_gmail_poll(name):
    """The stored poll trigger named by ``event`` when it watches the
    ``gmail.messages`` source, or None. A missing selector, unconfigured
    poll triggers, an unknown name and a non-gmail source (the generic poll
    connector owns those) fold together: the caller only distinguishes
    live-vs-fallback, so any storage hiccup folds too — sampling never
    raises for want of infrastructure (see docs/connector-coverage-audit.md)."""
    from src.dapier.triggers import poll_triggers

    if not name:
        return None
    try:
        item = poll_triggers.get_item(name)
    except Exception:
        return None
    if not item or str(item.get("source") or "") != "gmail.messages":
        return None
    return item


def _fetch_gmail_sample(event=None, connection_id=None, limit=DEFAULT_LIMIT):
    """The Gmail chip's sample pull: the newest message a stored gmail poll
    watches right now (``source: "live"``), else the newest recorded gmail
    run carrying ``message.received`` (``"history"``), else a documented
    example (``"synthetic"``).

    ``event`` names the stored poll trigger; the live pull runs the poll's
    own fetch once against the epoch watermark (read, never advances the
    parked cursor) and wraps the newest item in the envelope a real fire
    would publish. A live fetch that cannot run — no connection, an
    unreachable Gmail API, a trigger that has not fired yet — falls through
    to the recorded/documented sample instead of failing: a sample pull
    shows the payload shape, it never raises.
    """
    from src.dapier.triggers import poll_triggers

    name = str(event or "").strip().lower()
    item = _stored_gmail_poll(name)
    if item is not None:
        try:
            items, _next_cursor = _gmail_poll_fetch(item, GMAIL_EPOCH_CURSOR)
            envelope = (poll_triggers.event_for(item, items[-1])
                        if items else None)
        except Exception:
            envelope = None
        if envelope is not None:
            return {
                "sample": trigger_discovery.as_sample(envelope),
                "source": "live",
                "connection_id": item.get("connection_id") or None,
            }
    found = trigger_discovery.history_sample("gmail", event="message.received")
    if found is not None:
        return {"sample": found, "source": "history", "connection_id": connection_id}
    return {
        "sample": trigger_discovery.synthetic_sample(
            "gmail", "message.received", dict(_GMAIL_SYNTHETIC_MESSAGE)),
        "source": "synthetic",
        "connection_id": connection_id,
    }


register_trigger_discovery(TriggerDiscovery(
    connector="gmail", label="Gmail", kind="sample", resource="",
    fetch=_fetch_gmail_sample))
