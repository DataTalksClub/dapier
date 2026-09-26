"""Zapier-style email triggers: reserve an address and bind actions to it.

Creating a trigger reserves a local part at the trigger domain and stores the
actions to run for every message sent to that address. The address itself
works without per-address provisioning: SES accepts the whole trigger domain
(catch-all receipt rule) and the datamailer routes the local part as-is.
The worker merges stored triggers with the YAML workflows on every
invocation, so a created trigger is live without a deploy. Only reserved
names run actions; anything else at the domain matches no workflow.
"""

import logging
import os
import re
from datetime import datetime, timezone
from decimal import Decimal

logger = logging.getLogger(__name__)

TABLE_ENV = "EMAIL_TRIGGERS_TABLE"
DOMAIN_ENV = "TRIGGER_EMAIL_DOMAIN"
DEFAULT_DOMAIN = "dtcdev.click"

NAME_PATTERN = re.compile(r"^[a-z0-9][a-z0-9-]{0,30}[a-z0-9]$")
# Only infrastructure addresses are reserved here. Routes claimed by bundled
# YAML workflows are guarded separately: build_item refuses to shadow them
# (yaml_email_routes), so a route becomes app-manageable as soon as the YAML
# stops claiming it — no reserved-list change needed.
RESERVED_NAMES = {
    "abuse", "admin", "api", "auth", "billing", "dmarc", "datamailer", "e2e",
    "hostmaster", "imap", "mail", "no-reply", "noreply", "pop", "postmaster",
    "relay", "root", "security", "smtp", "support", "webmaster", "webmail",
    "www",
}

# Required and optional keys per action type live in the connector registry
# (src/dapier/connectors/registry.py); ACTION_SPECS reads them from there.
def __getattr__(name):
    if name == "ACTION_SPECS":
        from ..connectors import registry

        return registry.action_specs()
    raise AttributeError(name)


class TriggerError(ValueError):
    """Invalid or conflicting trigger definition."""


def trigger_domain():
    return os.environ.get(DOMAIN_ENV, DEFAULT_DOMAIN).strip().lstrip("@").lower()


def address_for(name):
    return f"{name}@{trigger_domain()}"


def validate_name(name):
    name = str(name or "").strip().lower()
    if not NAME_PATTERN.fullmatch(name):
        raise TriggerError("trigger name must be 2-32 chars: lowercase letters, digits, hyphens")
    if name in RESERVED_NAMES:
        raise TriggerError(f"the address {address_for(name)} is reserved")
    return name


def validate_actions(actions):
    """Validation lives in the connector registry (one spec set for the
    engine, the API and the designer); TriggerError is this module's dialect."""
    from ..connectors import registry

    try:
        return registry.validate_action_chain(actions)
    except registry.ActionError as exc:
        raise TriggerError(str(exc)) from None


def resolve_actions_flow(body):
    """Inline actions or a named shared flow — exactly one of the two.

    A flow must exist in the bundled YAML at save time. The reference is
    stored by name, so a later deploy that removes the flow fails the
    trigger closed (it stops matching) instead of running an empty chain.
    """
    flow = str(body.get("flow") or "").strip()
    actions = body.get("actions")
    if flow and actions:
        raise TriggerError("bind either inline actions or a flow, not both")
    if flow:
        from ..engine import matching

        if matching.flow_actions(flow) is None:
            raise TriggerError(f"no shared flow named '{flow}'")
        return None, flow
    return validate_actions(actions), ""


def flow_catalog():
    """The shared flows a trigger can bind to (from the bundled YAML)."""
    from ..engine import matching

    return matching.flow_catalog()


def yaml_email_routes():
    """Routes already claimed by YAML workflows, so triggers cannot shadow them."""
    from .. import engine

    routes = set()
    for workflow in engine.workflows():
        for trigger in engine.workflow_triggers(workflow):
            if trigger.get("connector") != "email":
                continue
            rule = (trigger.get("filters") or {}).get("route") or {}
            if isinstance(rule, dict) and rule.get("equals"):
                routes.add(str(rule["equals"]).lower())
    return routes


def build_item(body, operator):
    name = validate_name(body.get("name"))
    actions, flow = resolve_actions_flow(body)
    if name in yaml_email_routes():
        raise TriggerError(f"the route '{name}' is already handled by a YAML workflow")
    now = datetime.now(timezone.utc).isoformat()
    return {
        "name": name,
        "address": address_for(name),
        "description": str(body.get("description") or "")[:200],
        "actions": actions or [],
        "flow": flow,
        "enabled": bool(body.get("enabled", True)),
        "created_by": str(operator or ""),
        "created_at": now,
        "updated_at": now,
    }


def get_table(table_ref=None):
    if table_ref is not None:
        return table_ref
    name = os.environ.get(TABLE_ENV)
    if not name:
        raise TriggerError("email triggers are not configured")
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
        key=lambda item: item.get("name", ""),
    )


def workflow_for(item):
    """The engine workflow for a stored trigger, or None when its flow is gone."""
    actions = item.get("actions") or []
    flow = str(item.get("flow") or "").strip()
    if flow:
        from ..engine import matching

        actions = matching.flow_actions(flow)
        if actions is None:
            logger.warning("email trigger '%s' binds undefined flow '%s'; skipped",
                           item["name"], flow)
            return None
    return {
        "id": f"email-trigger-{item['name']}",
        "enabled": True,
        "trigger": {
            "connector": "email",
            "event": "message.received",
            "filters": {"route": {"equals": item["name"]}},
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
    return {key: item.get(key) for key in (
        "name", "address", "description", "actions", "flow", "enabled",
        "created_by", "created_at", "updated_at",
    )}


def api_list(table_ref=None):
    triggers = [public_view(item) for item in load_items(table_ref=table_ref)]
    return 200, {
        "domain": trigger_domain(),
        "triggers": triggers,
        "yaml_routes": sorted(yaml_email_routes()),
        "flows": flow_catalog(),
    }


def api_save(body, operator, table_ref=None):
    if not isinstance(body, dict):
        raise TriggerError("request body must be an object")
    item = build_item(body, operator)
    previous = get_table(table_ref).get_item(Key={"name": item["name"]}).get("Item")
    created = previous is None
    if previous:
        item["created_by"] = previous.get("created_by", item["created_by"])
        item["created_at"] = previous.get("created_at", item["created_at"])
    get_table(table_ref).put_item(Item=item)
    return 200, {"created": created, **public_view(item)}


def api_delete(name, operator, table_ref=None):
    name = validate_name(name)
    existing = get_table(table_ref).get_item(Key={"name": name}).get("Item")
    if not existing:
        raise TriggerError(f"no trigger named '{name}'")
    get_table(table_ref).delete_item(Key={"name": name})
    return 200, {"ok": True, "name": name, "address": existing.get("address") or address_for(name)}
