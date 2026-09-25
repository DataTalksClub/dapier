"""Zapier-style webhook and Telegram triggers: reserve a URL, bind actions.

Creating a webhook trigger reserves ``/hooks/webhook/{name}`` and generates
a random bearer token: callers POST the payload with
``Authorization: Bearer <token>`` and the token is verified with a
constant-time compare before anything runs. The published event carries the
parsed body, query parameters, and content type so downstream actions can
reference the caller's fields.

Creating a Telegram trigger binds one Telegram bot connection to
``/hooks/telegram/{name}``: Dapier registers the delivery URL with
``setWebhook`` and a per-trigger secret that Telegram echoes in
``X-Telegram-Bot-Api-Secret-Token`` on every update. Telegram keeps a single
webhook per bot, so one connection can drive at most one trigger.

Like email triggers, the worker merges stored hooks into the YAML workflows
on every invocation, so a created trigger is live without a deploy.
"""

import logging
import os
import secrets
from datetime import datetime, timezone
from decimal import Decimal

from ..connections.providers import telegram_api

logger = logging.getLogger(__name__)
from .email_triggers import (
    ACTION_SPECS, NAME_PATTERN, TriggerError, flow_catalog,
    resolve_actions_flow, validate_actions,
)

TABLE_ENV = "HOOK_TRIGGERS_TABLE"
BASE_URL_ENV = "HOOKS_BASE_URL"

KINDS = ("webhook", "telegram")
WEBHOOK_EVENT = "request.received"
TELEGRAM_EVENT = "message.received"
TOKEN_BYTES = 32


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


def build_item(body, operator, kind, previous=None):
    """Build the stored item for a create or edit.

    The bearer token survives edits (configure callers once) and rotates
    only when the request sets ``rotate_token``. Telegram triggers keep the
    connection binding and re-register the webhook with Telegram on every
    save so a replaced bot token heals the registration.
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


def load_items(table_ref=None):
    items = get_table(table_ref).scan(Limit=200).get("Items", [])
    return sorted(
        ({key: _decode_numbers(value) for key, value in item.items()} for item in items),
        key=lambda item: item.get("hook_id", ""),
    )


def get_item(name, table_ref=None):
    name = validate_name(name)
    item = get_table(table_ref).get_item(Key={"hook_id": name}).get("Item")
    return {key: _decode_numbers(value) for key, value in item.items()} if item else None


def _telegram_connection(connection_id, connections_table):
    """Load the connection a telegram trigger binds to, verifying it fits."""
    if connections_table is not None:
        table = connections_table
    else:
        name = os.environ.get("CONNECTIONS_TABLE")
        if not name:
            raise TriggerError("connections are not configured")
        import boto3

        table = boto3.resource("dynamodb").Table(name)
    connection = table.get_item(Key={"connection_id": connection_id}).get("Item")
    if not connection or connection.get("provider") != "telegram":
        raise TriggerError(f"connection '{connection_id}' is not a Telegram connection")
    if connection.get("status") != "connected":
        raise TriggerError(f"connection '{connection_id}' is not connected yet")
    return connection


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


def workflow_for(item):
    """The engine workflow for a stored hook, or None when its flow is gone."""
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
    return {
        "id": workflow_id_for(item),
        "enabled": True,
        "trigger": {
            "connector": kind,
            "event": WEBHOOK_EVENT if kind == "webhook" else TELEGRAM_EVENT,
            "filters": {"hook": {"equals": item["hook_id"]}},
        },
        "actions": actions,
    }


def load_workflows(table_ref=None):
    return [
        workflow for workflow in
        (workflow_for(item) for item in load_items(table_ref=table_ref) if item.get("enabled", True))
        if workflow is not None
    ]


def public_view(item):
    """Operator-facing view. The token is included on purpose: it is the
    credential callers must present, so it has to be retrievable to keep the
    hook configurable (rotate it to invalidate)."""
    view = {key: item.get(key) for key in (
        "hook_id", "kind", "url", "token", "description", "actions", "flow", "enabled",
        "created_by", "created_at", "updated_at",
    )}
    if item.get("kind") == "telegram":
        view["connection_id"] = item.get("connection_id")
        view["header"] = "x-telegram-bot-api-secret-token"
    else:
        view["header"] = "authorization"
        view["auth_scheme"] = "Bearer"
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
    if item["kind"] == "telegram":
        if item["enabled"]:
            _register_telegram(item, table_ref=table_ref,
                               connections_table=connections_table, transport=transport)
        elif not created:
            # Disabling an existing trigger releases the bot's single webhook
            # so another trigger (or nothing) can claim it.
            _unregister_telegram(item, connections_table=connections_table, transport=transport)
    get_table(table_ref).put_item(Item=item)
    return 200, {"created": created, **public_view(item)}


def api_delete(name, operator, kind=None, *, table_ref=None,
               connections_table=None, transport=None):
    item = get_item(name, table_ref=table_ref)
    if not item:
        raise TriggerError(f"no {kind or 'hook'} trigger named '{validate_name(name)}'")
    if kind and item.get("kind") != kind:
        raise TriggerError(f"trigger '{item['hook_id']}' is a {item['kind']} trigger")
    if item.get("kind") == "telegram" and item.get("connection_id"):
        # Best-effort: remove the delivery URL so Telegram stops POSTing to
        # a hook nobody listens on; the trigger is gone either way.
        _unregister_telegram(item, connections_table=connections_table, transport=transport)
    get_table(table_ref).delete_item(Key={"hook_id": item["hook_id"]})
    return 200, {"ok": True, "hook_id": item["hook_id"], "kind": item["kind"]}
