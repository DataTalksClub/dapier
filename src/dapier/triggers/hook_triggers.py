"""Zapier-style webhook and Telegram triggers: reserve a URL, bind actions.

Creating a webhook trigger reserves ``/hooks/webhook/{name}`` and generates
a random bearer token: callers POST the payload with
``Authorization: Bearer <token>`` and the token is verified with a
constant-time compare before anything runs. The published event carries the
parsed body, query parameters, and content type so downstream actions can
reference the caller's fields.

A webhook trigger saved with an optional ``secret`` locks the URL behind a
shared-secret HMAC check instead: callers sign the raw body —
``sha256=<hex of HMAC-SHA256(secret, raw body)>``, lowercase hex, in
``X-Dapier-Signature`` (bare hex tolerated) — and a valid signature alone
admits the delivery, constant-time verified; the bearer token alone is then
rejected with 401 and nothing is published. The same header and scheme the
outbound webhook action sends (``engine.actions.webhook``), mirrored inbound
for wary providers; a ``signature_header`` rename points the check at the
provider's own header (GitHub's ``X-Hub-Signature-256``, ...).

Creating a Telegram trigger binds one Telegram bot connection to
``/hooks/telegram/{name}``: Dapier registers the delivery URL with
``setWebhook`` and a per-trigger secret that Telegram echoes in
``X-Telegram-Bot-Api-Secret-Token`` on every update. Telegram keeps a single
webhook per bot, so one connection can drive at most one trigger.

Creating a Mailchimp trigger binds an audience (``list_id``) to
``/hooks/mailchimp/{name}``: Dapier subscribes the delivery URL on the
audience with Mailchimp's ``POST /lists/{id}/webhooks`` (authenticated with
the stored Mailchimp API key, the connection's or the shared ``mailchimp``
credential) for the trigger's subscribed event types. Mailchimp answers the
registration with a ``ping`` POST, which the intake answers 200 without
publishing. Unlike Telegram, Mailchimp has no delete-by-URL: unregistering
lists the audience's webhooks, finds ours by URL, and deletes it by id. Both
directions are best-effort: a Mailchimp failure never fails the local save or
delete — it comes back as a ``warnings`` list in the response instead.

Creating a YouTube trigger binds a channel (``channel_id``): Dapier
subscribes the channel on the WebSub hub right away (best-effort, like
Mailchimp a failure warns instead of blocking the save) and the renewal
schedule keeps the subscription alive. YouTube delivers new uploads to the
deployment's shared ``/hooks/youtube`` callback — verified against the hub
secret — so the trigger's own ``/hooks/youtube/{name}`` URL is its stored
identity, not a delivery target; the ``channel_id`` filter scopes what the
trigger matches. Disabling or deleting never unsubscribes: the callback is
shared by every watcher of the channel, and the renewal schedule only
re-subscribes channels live workflows still name, so a released channel's
subscription simply lapses.

Like email triggers, the worker merges stored hooks into the YAML workflows
on every invocation, so a created trigger is live without a deploy.
"""

import hashlib
import hmac
import json
import logging
import os
import re
import secrets
from datetime import datetime, timezone
from decimal import Decimal

from ..connections.providers import telegram_api
from .intake.mailchimp_webhooks import EVENT_TYPES as MAILCHIMP_EVENT_TYPES

logger = logging.getLogger(__name__)
from .email_triggers import (
    ACTION_SPECS, NAME_PATTERN, TriggerError, flow_catalog,
    resolve_actions_flow, validate_actions,
)

TABLE_ENV = "HOOK_TRIGGERS_TABLE"
BASE_URL_ENV = "HOOKS_BASE_URL"
# DynamoDB scan page size, not a cutoff: load_items walks pages to exhaustion.
SCAN_LIMIT = 200

KINDS = ("webhook", "telegram", "mailchimp", "youtube")
WEBHOOK_EVENT = "request.received"
TELEGRAM_EVENT = "message.received"
# Channel announcements (the Bot API's channel_post / edited_channel_post
# updates) are their own event, so a workflow can filter on the event name
# instead of poking at the raw update. Both events share the hook filter and
# every other filter rule — see telegram_event_for and workflow_for.
TELEGRAM_CHANNEL_POST_EVENT = "channel_post.received"
# Button taps (the Bot API's callback_query updates — the answer to an
# inline keyboard) are their own event, like channel announcements: same
# hook filter and fan-out, selectable by event name — see telegram_event_for
# and workflow_for.
TELEGRAM_CALLBACK_QUERY_EVENT = "callback_query.received"
YOUTUBE_EVENT = "video.published"
TOKEN_BYTES = 32

# Optional shared-secret HMAC check for webhook deliveries (webhook kind
# only): a trigger saved with a ``secret`` no longer accepts its bearer
# token alone — the caller must send the HMAC-SHA256 of the raw body in
# ``X-Dapier-Signature`` as ``sha256=<hex>`` (bare hex tolerated), verified
# constant-time. Same header and scheme the outbound webhook action signs
# with, mirrored for inbound deliveries.
SIGNATURE_HEADER = "x-dapier-signature"
SIGNATURE_PREFIX = "sha256="
# Providers sign deliveries in their own header (GitHub's
# X-Hub-Signature-256, ...), so a trigger may store ``signature_header`` —
# the name intake reads instead of SIGNATURE_HEADER.
SIGNATURE_HEADER_PATTERN = re.compile(r"[a-z0-9][a-z0-9.-]*")

# response.mode: how the hook URL answers a delivery (webhook kind only —
# Telegram requires a fast 200 or it retries the update forever, so its
# response stays a fixed ack). "ack" (the default) is today's behavior: the
# event is queued and the caller gets 202. "challenge" answers verification
# handshakes without running anything: 200 echoing the caller's challenge
# value (query `challenge`/`hub.challenge`, falling back to the body's
# `challenge`) as plain text. "sync" runs the matched workflow inline — the
# real production path (step leases, run history, task usage, failure
# notify), just without the queue hop — and answers with the outcome: a
# `template` (any JSON value; string leaves render with the same
# {steps.<id>.output.*} / {trigger.*} vocabulary actions use) when given,
# else {"ok", "workflow", "steps"}; a failed chain answers 500 and releases
# the delivery so a provider retry re-runs it, and a suspended run (delay
# past the inline cap) parks its continuation on the queue and answers 202
# {"suspended": true, "resume_at": ...}. Sync is attempted only when exactly
# one workflow matches the delivery — zero or several fall back to the ack
# path (202), whose behavior stays deterministic. The inline run gets a
# wall-clock budget (`budget_seconds`, default 10, cap 25 — the ingress
# function's own timeout is ~30 s) checked between steps, never mid-step: an
# overrun skips the un-run steps in place without side effects, enqueues the
# delivery under the same event id (the step leases the inline run wrote
# dedupe the worker's replay), and answers 202 — the worker finishes the run
# out of band. Longer chains belong in ack mode.
RESPONSE_MODES = ("ack", "challenge", "sync")

# Sync-run guardrails: the success status the hook URL answers (2xx), and
# the inline run's wall-clock budget in seconds.
RESPONSE_STATUS_BOUNDS = (200, 299)
RESPONSE_STATUS_DEFAULT = 200
RESPONSE_BUDGET_BOUNDS = (1, 25)
RESPONSE_BUDGET_DEFAULT_SECONDS = 10

# dedupe_path: optional per-trigger config naming the stable delivery id
# inside a delivered payload, so a provider retry of the same delivery is
# skipped instead of double-running the workflows. Convention: a dotted
# path into the parsed JSON payload the caller POSTed — "event.id" reads
# payload["event"]["id"], "data.delivery_id" reads payload["data"]["delivery_id"]
# (segments are [A-Za-z0-9_-]+). A delivery whose value at the path is
# missing or empty is never deduped: it runs normally. The value is hashed
# with the trigger id (dedupe_event_id) and claimed in the seen store
# (triggers.seen, cursors table) for the TTL window (7 days): the first
# delivery publishes and runs, a retry of the same value is answered 202
# without a new run. Headers are not addressable — they are not part of
# the payload stored with the event.
DEDUPE_PATH_PATTERN = re.compile(r"[A-Za-z0-9_-]+(\.[A-Za-z0-9_-]+)*")


def base_url():
    return os.environ.get(BASE_URL_ENV, "").rstrip("/")


def hook_url(kind, name):
    return f"{base_url()}/hooks/{kind}/{name}"


def validate_name(name):
    name = str(name or "").strip().lower()
    if not NAME_PATTERN.fullmatch(name):
        raise TriggerError("hook name must be 2-32 chars: lowercase letters, digits, hyphens")
    return name


def new_token():
    return secrets.token_urlsafe(TOKEN_BYTES)


def workflow_id_for(item):
    return f"{item['kind']}-trigger-{item['hook_id']}"


def validate_dedupe_path(value):
    """The validated ``dedupe_path`` (or "" when unset) for a save.

    Accepts only dotted identifier paths — a wrong type (number, object) or
    a path that can never resolve into a payload ("", "a..b", "!") is
    rejected at save time on both the admin and agent routes (they share
    ``build_item`` through ``api_save``).
    """
    if value is not None and not isinstance(value, str):
        raise TriggerError("dedupe_path must be a dotted path string like 'data.delivery_id'")
    path = str(value or "").strip()
    if path and not DEDUPE_PATH_PATTERN.fullmatch(path):
        raise TriggerError("dedupe_path must be a dotted path like 'data.delivery_id'")
    return path


def dedupe_value(payload, path):
    """The value at a dot-path in a delivered payload, or None."""
    value = payload
    for part in [segment for segment in str(path or "").split(".") if segment]:
        if not isinstance(value, dict) or part not in value:
            return None
        value = value[part]
    return value


def dedupe_event_id(hook_id, value):
    """The stable event id for one dedupe value (the poll id shape: the
    trigger's name plus a hash of the value, instead of a fresh uuid per
    POST), so a provider retry of the same delivery publishes — and
    therefore runs — under the id it already used."""
    return f"{hook_id}-{hashlib.sha256(str(value).encode()).hexdigest()[:16]}"


def validate_secret(value, kind):
    """The validated shared secret (or "" when unset) for a hook save.

    Optional and webhook-only: a trigger saved with one locks its URL
    behind the signature check (``verify_signature``) — the bearer token
    alone no longer admits a delivery. Non-webhook kinds never verify
    signatures, so a secret there is rejected rather than silently
    ignored. An omitted value on an edit keeps the stored secret (the
    binding survives the save, like Telegram's ``connection_id``); an
    explicit ``""`` or ``null`` clears it. The value is write-only:
    ``public_view`` exposes ``signed: true``, never the secret.
    """
    if value is None:
        value = ""
    if not isinstance(value, str):
        raise TriggerError("secret must be a string")
    secret = value.strip()
    if secret and kind != "webhook":
        raise TriggerError(
            f"{kind} triggers do not verify signatures; secret is webhook-only")
    return secret


def validate_signature_header(value, kind):
    """The validated custom signature header name (or "" for the default).

    Part of the same webhook-only binding as ``secret`` — the header is
    meaningless without one, so a non-empty value on another kind is
    rejected rather than silently ignored. Stored lowercase; an empty value
    reads as ``SIGNATURE_HEADER`` at read time. An omitted value on an edit
    keeps the stored name, like ``secret`` (the binding survives the save).
    """
    if value is None:
        value = ""
    if not isinstance(value, str):
        raise TriggerError("signature_header must be a string")
    header = value.strip().lower()
    if header and kind != "webhook":
        raise TriggerError(
            f"{kind} triggers do not verify signatures; signature_header is webhook-only")
    if header and not SIGNATURE_HEADER_PATTERN.fullmatch(header):
        raise TriggerError(
            "signature_header must be a header name like 'x-hub-signature-256'")
    return header


def signature_header_for(item):
    """The header whose value must carry the delivery's signature — the
    trigger's stored custom name, else ``SIGNATURE_HEADER``."""
    return str(item.get("signature_header") or "").strip().lower() or SIGNATURE_HEADER


def signature_for(secret, body):
    """The lowercase hex HMAC-SHA256(secret, raw body) a signed delivery
    carries. Shared by the intake (``api.router._webhook_hook``) and the
    tests, so a verified delivery is hashed exactly as the docs' examples
    compute it."""
    return hmac.new(str(secret).encode(), body, hashlib.sha256).hexdigest()


def verify_signature(secret, body, header):
    """Constant-time check of the signature header value on a raw delivery
    body (the header name is the caller's — ``signature_header_for``).

    The value carries ``sha256=<hex>`` — the scheme prefix is
    case-insensitive, the hex must be the lowercase HMAC-SHA256 digest of
    the body exactly as received (bare hex is accepted too). Anything else
    — a missing header, another scheme, a mismatch — is False.
    """
    value = str(header or "").strip()
    if value.lower().startswith(SIGNATURE_PREFIX):
        value = value[len(SIGNATURE_PREFIX):].strip()
    return hmac.compare_digest(value.lower(), signature_for(secret, body))


def validate_response(value, kind):
    """The validated ``response`` config (or ``{"mode": "ack"}``) for a save.

    Webhook triggers only: ``mode`` must be one of RESPONSE_MODES; optional
    ``template`` is any JSON value (rendered at delivery time for sync
    mode), optional ``status`` the 2xx success code sync answers with
    (default 200), and optional ``budget_seconds`` the inline run's
    wall-clock budget (1-25, clamped, default 10). Keys are stored only
    when set, so configs saved before a key existed read as its default.
    Telegram triggers must not set it — their caller (Telegram itself)
    needs the fast fixed ack — and neither do youtube triggers: their
    deliveries land on the shared WebSub callback, never on the stored URL,
    so there is no response to shape.
    """
    if value is None:
        return {"mode": "ack"}
    if kind in ("telegram", "youtube"):
        raise TriggerError(f"{kind} triggers do not answer their URL; response is webhook-only")
    if not isinstance(value, dict):
        raise TriggerError("response must be an object like {mode: sync, template: ...}")
    allowed = {"mode", "template", "status", "budget_seconds"}
    unknown = set(value) - allowed
    if unknown:
        raise TriggerError(
            f"response accepts only {', '.join(sorted(allowed))} (unknown: {', '.join(sorted(unknown))})")
    mode = value.get("mode") or "ack"
    if mode not in RESPONSE_MODES:
        raise TriggerError(f"response.mode must be one of: {', '.join(RESPONSE_MODES)}")
    response = {"mode": mode}
    if value.get("template") is not None:
        try:
            json.dumps(value["template"])
        except (TypeError, ValueError):
            raise TriggerError("response.template must be JSON-serializable")
        response["template"] = value["template"]
    if "status" in value:
        status = value["status"]
        if (isinstance(status, bool) or not isinstance(status, (int, float))
                or int(status) != status
                or not RESPONSE_STATUS_BOUNDS[0] <= int(status) <= RESPONSE_STATUS_BOUNDS[1]):
            raise TriggerError(
                f"response.status must be a 2xx status code "
                f"({RESPONSE_STATUS_BOUNDS[0]}-{RESPONSE_STATUS_BOUNDS[1]})")
        response["status"] = int(status)
    if "budget_seconds" in value:
        budget = value["budget_seconds"]
        if isinstance(budget, bool) or not isinstance(budget, (int, float)):
            raise TriggerError("response.budget_seconds must be a number of seconds (1-25)")
        response["budget_seconds"] = max(RESPONSE_BUDGET_BOUNDS[0],
                                         min(int(budget), RESPONSE_BUDGET_BOUNDS[1]))
    return response


def response_status(item):
    """The sync success status stored on a trigger item, or the default.

    Read-time twin of the ``status`` validation: a hand-edited or pre-key
    item never fails a delivery, it reads as the default.
    """
    value = (item.get("response") or {}).get("status") if isinstance(item, dict) else None
    try:
        value = int(value)
    except (TypeError, ValueError):
        return RESPONSE_STATUS_DEFAULT
    return value if RESPONSE_STATUS_BOUNDS[0] <= value <= RESPONSE_STATUS_BOUNDS[1] \
        else RESPONSE_STATUS_DEFAULT


def response_budget(item):
    """The inline run's wall-clock budget in seconds, clamped to bounds.

    Same read-time contract as ``response_status``: anything unreadable or
    out of range lands on the default (10) or the cap (25).
    """
    value = (item.get("response") or {}).get("budget_seconds") if isinstance(item, dict) else None
    try:
        value = int(value)
    except (TypeError, ValueError):
        return RESPONSE_BUDGET_DEFAULT_SECONDS
    return max(RESPONSE_BUDGET_BOUNDS[0], min(value, RESPONSE_BUDGET_BOUNDS[1]))


def validate_mailchimp_events(value, previous=None):
    """The validated subscribed-event list for a mailchimp trigger.

    Any of the intake's event types (the set Mailchimp POSTs); the default —
    and an omitted or empty list on a create — is all of them. On an edit the
    stored list survives an omitted ``events``, mirroring how the Telegram
    binding keeps its ``connection_id``.
    """
    if value is None:
        value = (previous or {}).get("events")
    if value is None:
        return list(MAILCHIMP_EVENT_TYPES)
    if not isinstance(value, list) or not value:
        raise TriggerError(
            "events must be a list of mailchimp webhook types: "
            f"{', '.join(MAILCHIMP_EVENT_TYPES)}")
    events = []
    for entry in value:
        name = str(entry or "").strip().lower()
        if name not in MAILCHIMP_EVENT_TYPES:
            raise TriggerError(
                f"unknown mailchimp event '{entry}'; subscribed types: "
                f"{', '.join(MAILCHIMP_EVENT_TYPES)}")
        if name not in events:
            events.append(name)
    return events or list(MAILCHIMP_EVENT_TYPES)


def build_item(body, operator, kind, previous=None):
    """Build the stored item for a create or edit.

    The bearer token survives edits (configure callers once) and rotates
    only when the request sets ``rotate_token``. Telegram triggers keep the
    connection binding and re-register the webhook with Telegram on every
    save so a replaced bot token heals the registration. YouTube triggers
    keep the watched channel and re-subscribe it on every enabled save (the
    hub call is idempotent and refreshes the subscription lease).
    """
    if kind not in KINDS:
        raise TriggerError(f"hook kind must be one of: {', '.join(KINDS)}")
    if not isinstance(body, dict):
        raise TriggerError("request body must be an object")
    name = validate_name(body.get("name"))
    if previous and previous.get("kind") != kind:
        raise TriggerError(
            f"the name '{name}' is already used by a {previous.get('kind')} trigger")
    actions, flow = resolve_actions_flow(body)
    previous = previous or {}
    created = not previous
    token = new_token() if (created or body.get("rotate_token")) else previous.get("token")
    item = {
        "hook_id": name,
        "kind": kind,
        "url": hook_url(kind, name),
        "token": token,
        "description": str(body.get("description") or "")[:200],
        "dedupe_path": validate_dedupe_path(body.get("dedupe_path")),
        "secret": validate_secret(
            body["secret"] if "secret" in body else previous.get("secret"), kind),
        "signature_header": validate_signature_header(
            body["signature_header"] if "signature_header" in body
            else previous.get("signature_header"), kind),
        "response": validate_response(body.get("response"), kind),
        "actions": actions or [],
        "flow": flow,
        "enabled": bool(body.get("enabled", True)),
        "created_by": previous.get("created_by") or str(operator or ""),
        "created_at": previous.get("created_at") or datetime.now(timezone.utc).isoformat(),
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    if kind == "telegram":
        connection_id = str(body.get("connection_id") or previous.get("connection_id") or "").strip()
        if not connection_id:
            raise TriggerError("a telegram trigger requires the connection_id of a Telegram bot connection")
        item["connection_id"] = connection_id
    elif kind == "mailchimp":
        list_id = str(body.get("list_id") or previous.get("list_id") or "").strip()
        if not list_id:
            raise TriggerError("a mailchimp trigger requires the list_id of the audience to watch")
        item["list_id"] = list_id
        item["events"] = validate_mailchimp_events(body.get("events"), previous)
        item["connection_id"] = str(
            body.get("connection_id") or previous.get("connection_id") or "").strip()
    elif kind == "youtube":
        channel_id = str(body.get("channel_id") or previous.get("channel_id") or "").strip()
        if not channel_id:
            raise TriggerError("a youtube trigger requires the channel_id of the YouTube channel to watch")
        item["channel_id"] = channel_id
    return item, created


def get_table(table_ref=None):
    if table_ref is not None:
        return table_ref
    name = os.environ.get(TABLE_ENV)
    if not name:
        raise TriggerError("hook triggers are not configured")
    import boto3

    return boto3.resource("dynamodb").Table(name)


def _decode_numbers(value):
    """DynamoDB's resource API returns Decimals; engine params must stay JSON-safe."""
    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral_value() else float(value)
    if isinstance(value, dict):
        return {key: _decode_numbers(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_decode_numbers(item) for item in value]
    return value


def _scan_all(table):
    """Read every scan page: a single Limit=200 scan silently dropped
    trigger #201 and beyond — it would stop firing with no error anywhere.
    Same walk as published_workflows._scan_all (the managed-store loader)."""
    items, start = [], None
    while True:
        kwargs = {"Limit": SCAN_LIMIT}
        if start:
            kwargs["ExclusiveStartKey"] = start
        page = table.scan(**kwargs)
        items.extend(page.get("Items", []))
        start = page.get("LastEvaluatedKey")
        if not start:
            return items


def load_items(table_ref=None):
    return sorted(
        ({key: _decode_numbers(value) for key, value in item.items()}
         for item in _scan_all(get_table(table_ref))),
        key=lambda item: item.get("hook_id", ""),
    )


def get_item(name, table_ref=None):
    name = validate_name(name)
    item = get_table(table_ref).get_item(Key={"hook_id": name}).get("Item")
    return {key: _decode_numbers(value) for key, value in item.items()} if item else None


def telegram_event_for(update):
    """The event name one raw Telegram update publishes as.

    Channel announcements (``channel_post`` / ``edited_channel_post``) are
    their own event — ``channel_post.received`` — and inline-keyboard button
    taps (``callback_query``) another — ``callback_query.received`` — so a
    workflow can select them by event name; every other update stays
    ``message.received``. Shared by the ingress hook
    (``api.router._telegram_hook``) and live trigger discovery, so a
    discovered sample names the event its payload really arrives as.
    """
    if isinstance(update, dict):
        if update.get("channel_post") or update.get("edited_channel_post"):
            return TELEGRAM_CHANNEL_POST_EVENT
        if update.get("callback_query"):
            return TELEGRAM_CALLBACK_QUERY_EVENT
    return TELEGRAM_EVENT


def _telegram_callback_data(update, hook_id):
    """The flattened event data for one callback_query update (see
    update_data). Key names match the message events for what they share
    (hook, update_id, message_id, text, entities, chat_id, chat, from,
    update) — the originating message, when Telegram attaches one — plus
    the tap's own fields: ``id`` (what an answerCallbackQuery call echoes),
    ``data`` (the button's payload string a workflow branches on) and
    ``inline_message_id`` (present instead of ``message`` for keyboards on
    messages sent via the inline mode)."""
    query = update.get("callback_query") or {}
    message = query.get("message") or {}
    chat = message.get("chat") or {}
    return {
        "hook": hook_id,
        "update_id": update.get("update_id"),
        "id": query.get("id"),
        "data": query.get("data") or "",
        "from": query.get("from") or {},
        "inline_message_id": query.get("inline_message_id"),
        "message_id": message.get("message_id"),
        "text": message.get("text") or message.get("caption") or "",
        "entities": message.get("entities") or message.get("caption_entities") or [],
        "chat_id": chat.get("id"),
        "chat": chat,
        "update": update,
    }


def update_data(update, hook_id):
    """The published event data for one raw Telegram update.

    Shared by the ingress hook (``api.router._telegram_hook``) and live
    trigger discovery, so a discovered sample is exactly the shape a real
    delivery publishes. Channel posts (announcement channels) carry no
    "from"; text may arrive as a media caption with caption_entities
    instead of entities; a button tap (callback_query) flattens into its
    own shape — see _telegram_callback_data.
    """
    if isinstance(update, dict) and update.get("callback_query"):
        return _telegram_callback_data(update, hook_id)
    message = (update.get("message") or update.get("edited_message")
               or update.get("channel_post") or update.get("edited_channel_post") or {})
    chat = message.get("chat") or {}
    sender = message.get("from") or {}
    return {
        "hook": hook_id,
        "update_id": update.get("update_id"),
        "message_id": message.get("message_id"),
        "text": message.get("text") or message.get("caption") or "",
        "entities": message.get("entities") or message.get("caption_entities") or [],
        "is_channel_post": bool(update.get("channel_post") or update.get("edited_channel_post")),
        "chat_id": chat.get("id"),
        "chat": chat,
        "from": sender,
        "update": update,
    }


def _connected_connection(connection_id, connections_table, provider):
    """Load the connection a trigger binds to, verifying provider and status."""
    if connections_table is not None:
        table = connections_table
    else:
        name = os.environ.get("CONNECTIONS_TABLE")
        if not name:
            raise TriggerError("connections are not configured")
        import boto3

        table = boto3.resource("dynamodb").Table(name)
    connection = table.get_item(Key={"connection_id": connection_id}).get("Item")
    if not connection or connection.get("provider") != provider:
        raise TriggerError(f"connection '{connection_id}' is not a {provider.capitalize()} connection")
    if connection.get("status") != "connected":
        raise TriggerError(f"connection '{connection_id}' is not connected yet")
    return connection


def _telegram_connection(connection_id, connections_table):
    """Load the connection a telegram trigger binds to, verifying it fits."""
    return _connected_connection(connection_id, connections_table, "telegram")


def _mailchimp_connection(connection_id, connections_table):
    """Load the connection a mailchimp trigger binds to, verifying it fits."""
    return _connected_connection(connection_id, connections_table, "mailchimp")


def _telegram_connection_token(connection, connections_table):
    """The stored bot token for a connection, from the credentials store."""
    from ..connections.credentials import get_credential

    try:
        return get_credential(connection["credential_id"]).get("token")
    except KeyError:
        raise TriggerError(
            f"connection '{connection['connection_id']}' has no stored bot token; reconnect it"
        )


def _register_telegram(item, *, table_ref=None, connections_table=None, transport=None):
    """Bind the bot's delivery URL to this trigger, enforcing one-per-bot."""
    connection = _telegram_connection(item["connection_id"], connections_table)
    owners = [
        other["hook_id"] for other in load_items(table_ref=table_ref)
        if other.get("kind") == "telegram"
        and other.get("connection_id") == item["connection_id"]
        and other.get("hook_id") != item["hook_id"]
        and other.get("enabled", True)
    ]
    if owners:
        raise TriggerError(
            f"the bot behind connection '{item['connection_id']}' already drives "
            f"trigger '{owners[0]}'; Telegram allows one webhook per bot"
        )
    telegram_api.set_webhook(
        _telegram_connection_token(connection, connections_table),
        item["url"], item["token"],
        transport=transport,
    )


def _unregister_telegram(item, *, connections_table=None, transport=None):
    """Best-effort release of the bot's delivery URL; failures never block the save."""
    try:
        connection = _telegram_connection(item["connection_id"], connections_table)
        telegram_api.delete_webhook(
            _telegram_connection_token(connection, connections_table),
            transport=transport,
        )
    except (TriggerError, telegram_api.TelegramApiError):
        pass


def _mailchimp_api_settings(item, connections_table):
    """The (api_key, server) pair behind a mailchimp trigger: the bound
    connection's stored credential, else the shared ``mailchimp`` credential
    (the same resolution the connector's audience fetchers use)."""
    from ..connectors.mailchimp import _stored_settings

    connection = {}
    if item.get("connection_id"):
        connection = _mailchimp_connection(item["connection_id"], connections_table)
    try:
        return _stored_settings(connection)
    except RuntimeError as exc:
        raise TriggerError(str(exc)) from None


def _register_mailchimp(item, *, connections_table=None, transport=None):
    """Subscribe the trigger's callback URL on its audience. Best-effort:
    returns a warning for the save response instead of raising, so a
    Mailchimp outage or a missing stored key never fails the local save —
    the trigger stays live for local merging and the operator sees what did
    not land remotely. Mailchimp webhooks are per-audience and additive, so
    registration first removes any webhook already registered under this
    trigger's URL (it may carry stale subscribed types), then posts the
    current one."""
    from ..connectors import mailchimp as mailchimp_connector

    try:
        api_key, server = _mailchimp_api_settings(item, connections_table)
        for entry in mailchimp_connector.list_webhooks(
                item["list_id"], api_key, server, transport=transport):
            if entry.get("url") == item["url"]:
                mailchimp_connector.delete_webhook(
                    item["list_id"], entry.get("id"), api_key, server, transport=transport)
        mailchimp_connector.register_webhook(
            item["list_id"], item["url"],
            {name: name in item["events"] for name in MAILCHIMP_EVENT_TYPES},
            api_key, server, transport=transport)
    except Exception as exc:
        return f"the trigger saved, but Mailchimp webhook registration failed: {exc}"
    return None


def _unregister_mailchimp(item, *, connections_table=None, transport=None):
    """Best-effort unsubscribe of the trigger's callback URL: Mailchimp has
    no delete-by-URL, so the audience's webhooks are listed, ours is found by
    URL, and deleted by id. Returns a warning for the response, or None when
    the registration is gone — every failure logs and continues, since the
    trigger is gone either way and Mailchimp must never block its deletion."""
    from ..connectors import mailchimp as mailchimp_connector

    try:
        api_key, server = _mailchimp_api_settings(item, connections_table)
        for entry in mailchimp_connector.list_webhooks(
                item["list_id"], api_key, server, transport=transport):
            if entry.get("url") == item["url"]:
                mailchimp_connector.delete_webhook(
                    item["list_id"], entry.get("id"), api_key, server, transport=transport)
    except Exception as exc:
        logger.warning("mailchimp trigger '%s': leaving its webhook registered (%s)",
                       item.get("hook_id"), exc)
        return f"Mailchimp webhook removal failed: {exc}"
    return None


def _register_youtube(item, *, transport=None):
    """Best-effort WebSub subscribe for the trigger's channel at save time.

    The renewal schedule (triggers.intake.youtube_subscriptions) would pick
    the channel up within its cycle anyway; subscribing on the enabled save
    means a fresh trigger can fire immediately instead of waiting days.
    Subscribing is idempotent — a re-save refreshes the hub lease — and a
    hub failure warns instead of blocking the local save, like Mailchimp's.

    Disabling and deleting never unsubscribe: the hub callback is shared by
    every watcher of the channel (stored hooks and designer workflows
    alike), so tearing the subscription down could silence workflows that
    still watch it. The renewal schedule only re-subscribes channels live
    workflows still name, so a released channel's lease simply lapses.
    """
    from .intake import youtube_subscriptions

    try:
        youtube_subscriptions.subscribe_channel(item["channel_id"], transport=transport)
    except Exception as exc:
        logger.warning("youtube trigger '%s': WebSub subscribe failed (%s)",
                       item.get("hook_id"), exc)
        return f"the trigger saved, but YouTube WebSub subscription failed: {exc}"
    return None


def workflow_for(item):
    """The engine workflow for a stored hook, or None when its flow is gone.

    A mailchimp trigger fans out into one trigger spec per subscribed event
    type — the intake publishes ``event`` named by the delivery's ``type``
    (subscribe, unsubscribe, ...) and matching is exact on event — each
    scoped to this hook by the ``hook`` field the intake puts in the data.

    A youtube trigger filters on ``channel_id`` instead: WebSub delivers to
    one shared callback (api.router._youtube) whose envelopes carry no hook
    field, so the channel is the scoping — like two designer workflows
    watching one channel, two youtube triggers on it both fire, which is
    normal trigger fan-out.
    """
    kind = item.get("kind")
    actions = item.get("actions") or []
    flow = str(item.get("flow") or "").strip()
    if flow:
        from ..engine import matching

        actions = matching.flow_actions(flow)
        if actions is None:
            logger.warning("hook trigger '%s' binds undefined flow '%s'; skipped",
                           item.get("hook_id"), flow)
            return None
    filters = {"hook": {"equals": item["hook_id"]}}
    if kind == "mailchimp":
        triggers = [
            {"connector": kind, "event": name, "filters": dict(filters)}
            for name in (item.get("events") or list(MAILCHIMP_EVENT_TYPES))
        ]
    elif kind == "youtube":
        triggers = [{
            "connector": "youtube",
            "event": YOUTUBE_EVENT,
            "filters": {"channel_id": {"equals": item["channel_id"]}},
        }]
    elif kind == "telegram":
        # One trigger spec per telegram event, like the mailchimp fan-out:
        # the same stored trigger (and therefore the same filters a workflow
        # author added — chat_id, text prefix, ...) matches a direct message,
        # a channel announcement and a button tap alike, while the event
        # name stays selectable for workflows that want only one of them.
        triggers = [
            {"connector": kind, "event": name, "filters": dict(filters)}
            for name in (TELEGRAM_EVENT, TELEGRAM_CHANNEL_POST_EVENT,
                         TELEGRAM_CALLBACK_QUERY_EVENT)
        ]
    else:
        triggers = [{
            "connector": kind,
            "event": WEBHOOK_EVENT,
            "filters": filters,
        }]
    return {
        "id": workflow_id_for(item),
        "enabled": True,
        "triggers": triggers,
        "actions": actions,
    }


def load_workflows(table_ref=None):
    return [
        workflow for workflow in
        (workflow_for(item) for item in load_items(table_ref=table_ref) if item.get("enabled", True))
        if workflow is not None
    ]


def _item_for_workflow_id(workflow_id, table_ref=None):
    """The stored hook projecting ``workflow_id`` (``<kind>-trigger-<hook>``), or None."""
    if not os.environ.get(TABLE_ENV):
        return None
    items = load_items() if table_ref is None else load_items(table_ref=table_ref)
    for item in items:
        if workflow_id_for(item) == workflow_id:
            return item
    return None


def workflow_by_id(workflow_id, visible=None, table_ref=None):
    """One hook-backed engine workflow by its workflow id, or None.

    The single-workflow mirror of listed_workflows: the designer and CLI
    read a trigger-run workflow through it, projected exactly as the list
    projects it (enabled state included, tokens never). ``visible`` (an
    auth.visibility.Visibility, None = unrestricted) scopes the read by the
    trigger's creator — a workflow the caller may not see answers None,
    like the list hiding the row.
    """
    item = _item_for_workflow_id(str(workflow_id or ""), table_ref)
    if item is None:
        return None
    if visible is not None and not visible.owner_visible(str(item.get("created_by") or "")):
        return None
    workflow = workflow_for(item)
    if workflow is None:
        return None
    return {**workflow, "enabled": item.get("enabled", True)}


def owns_workflow_id(workflow_id, table_ref=None):
    """The kind of the hook trigger projecting ``workflow_id``, or None.

    The engine prefers managed workflows — a published definition under a
    trigger's id silently takes over its routing (matching.all_workflows
    drops the hook duplicate). Saves refuse such an id so a designer save
    cannot rewire a live trigger by accident."""
    item = _item_for_workflow_id(workflow_id, table_ref)
    return str(item.get("kind") or "hook") if item else None


def listed_workflows(visible=None):
    """Hook-backed workflows for Console and CLI, including disabled hooks.

    Project through the engine definition so tokens and provider setup never
    escape into workflow lists. Stored hooks remain managed by the trigger API.
    """
    if not os.environ.get("HOOK_TRIGGERS_TABLE"):
        return []
    result = []
    for item in load_items():
        owner = str(item.get("created_by") or "")
        if visible is not None and not visible.owner_visible(owner):
            continue
        workflow = workflow_for(item)
        if workflow is not None:
            result.append(({**workflow, "enabled": item.get("enabled", True)}, owner))
    return result


def public_view(item):
    """Operator-facing view. The token is included on purpose: it is the
    credential callers must present, so it has to be retrievable to keep the
    hook configurable (rotate it to invalidate)."""
    view = {key: item.get(key) for key in (
        "hook_id", "kind", "url", "token", "description", "dedupe_path",
        "response", "actions", "flow", "enabled", "created_by", "created_at", "updated_at",
    )}
    if item.get("kind") == "telegram":
        view["connection_id"] = item.get("connection_id")
        view["header"] = "x-telegram-bot-api-secret-token"
    elif item.get("kind") == "mailchimp":
        # The binding an edit must round-trip (like Telegram's bot). No
        # header/auth_scheme: Mailchimp sends no auth headers — the
        # unguessable URL is the credential.
        view["list_id"] = item.get("list_id")
        view["events"] = item.get("events") or list(MAILCHIMP_EVENT_TYPES)
        if item.get("connection_id"):
            view["connection_id"] = item.get("connection_id")
    elif item.get("kind") == "youtube":
        # The watched channel is the binding an edit must round-trip. No
        # header/auth_scheme: YouTube delivers to the deployment's shared
        # /hooks/youtube callback, verified against the hub secret — the
        # stored URL is the trigger's identity, not a delivery target.
        view["channel_id"] = item.get("channel_id")
    else:
        view["header"] = "authorization"
        view["auth_scheme"] = "Bearer"
        if item.get("secret"):
            # The secret is write-only: callers see that the URL is
            # signature-locked (and which header carries the digest), never
            # the value.
            view["signed"] = True
            view["signature_header"] = signature_header_for(item)
    return view


def api_list(table_ref=None, kind=None):
    items = load_items(table_ref=table_ref)
    if kind:
        items = [item for item in items if item.get("kind") == kind]
    return 200, {
        "base_url": base_url(),
        "hooks": [public_view(item) for item in items],
        "flows": flow_catalog(),
    }


def api_save(body, operator, kind="webhook", *, table_ref=None,
             connections_table=None, transport=None):
    item, created = build_item(body, operator, kind,
                               previous=get_item(body.get("name"), table_ref=table_ref)
                               if isinstance(body, dict) and body.get("name") else None)
    warnings = []
    if item["kind"] == "telegram":
        if item["enabled"]:
            _register_telegram(item, table_ref=table_ref,
                               connections_table=connections_table, transport=transport)
        elif not created:
            # Disabling an existing trigger releases the bot's single webhook
            # so another trigger (or nothing) can claim it.
            _unregister_telegram(item, connections_table=connections_table, transport=transport)
    elif item["kind"] == "mailchimp":
        # Best-effort lifecycle with Mailchimp: every enabled save (re-)draws
        # the webhook registration, a Mailchimp failure warns instead of
        # blocking the local save, and disabling releases the subscription
        # like Telegram's.
        if item["enabled"]:
            warning = _register_mailchimp(item, connections_table=connections_table,
                                          transport=transport)
        elif not created:
            warning = _unregister_mailchimp(item, connections_table=connections_table,
                                            transport=transport)
        else:
            warning = None
        if warning:
            warnings.append(warning)
    elif item["kind"] == "youtube" and item["enabled"]:
        # Best-effort subscribe-on-save: a hub failure warns but the trigger
        # lands, and a disable/delete never unsubscribes (see
        # _register_youtube — the renewal schedule releases the channel).
        warning = _register_youtube(item, transport=transport)
        if warning:
            warnings.append(warning)
    get_table(table_ref).put_item(Item=item)
    payload = {"created": created, **public_view(item)}
    if warnings:
        payload["warnings"] = warnings
    return 200, payload


def api_delete(name, operator, kind=None, *, table_ref=None,
               connections_table=None, transport=None):
    item = get_item(name, table_ref=table_ref)
    if not item:
        raise TriggerError(f"no {kind or 'hook'} trigger named '{validate_name(name)}'")
    if kind and item.get("kind") != kind:
        raise TriggerError(f"trigger '{item['hook_id']}' is a {item['kind']} trigger")
    warnings = []
    if item.get("kind") == "telegram" and item.get("connection_id"):
        # Best-effort: remove the delivery URL so Telegram stops POSTing to
        # a hook nobody listens on; the trigger is gone either way.
        _unregister_telegram(item, connections_table=connections_table, transport=transport)
    elif item.get("kind") == "mailchimp":
        # Best-effort: unsubscribe so Mailchimp stops POSTing into a 404;
        # a Mailchimp failure warns but never blocks the deletion.
        warning = _unregister_mailchimp(item, connections_table=connections_table,
                                        transport=transport)
        if warning:
            warnings.append(warning)
    get_table(table_ref).delete_item(Key={"hook_id": item["hook_id"]})
    payload = {"ok": True, "hook_id": item["hook_id"], "kind": item["kind"]}
    if warnings:
        payload["warnings"] = warnings
    return 200, payload
