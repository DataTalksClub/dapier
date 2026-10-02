"""Read-only compatibility for legacy email-trigger records.

Email flows are now authored only as managed workflows. This module reads old
records until explicit API/CLI migration converts them, and retains the shared
name/action validation helpers used by the other trigger adapters. No email
CRUD writer is exposed; historical record construction exists only for legacy
validation. The receiving domain is accepted by upstream Datamailer/SES.
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
# DynamoDB scan page size, not a cutoff: load_items walks pages to exhaustion.
SCAN_LIMIT = 200

NAME_PATTERN = re.compile(r"^[a-z0-9][a-z0-9-]{0,30}[a-z0-9]$")
# Only infrastructure addresses are reserved here. Routes claimed by managed
# workflows are guarded separately: build_item refuses to shadow them
# (yaml_email_routes), so a route becomes app-manageable as soon as the
# workflow stops claiming it.
RESERVED_NAMES = {
    "abuse", "admin", "api", "auth", "billing", "dmarc", "datamailer", "e2e",
    "hostmaster", "imap", "mail", "no-reply", "noreply", "pop", "postmaster",
    "relay", "root", "security", "smtp", "support", "webmaster", "webmail",
    "www",
}

# The events a stored trigger can watch. message.received keeps the
# reserved-address behavior; the SES feedback watchers fire from the SNS
# intake for the whole domain, no address needed (see module docstring).
ADDRESS_EVENT = "message.received"
BOUNCE_EVENT = "bounce.received"
COMPLAINT_EVENT = "complaint.received"
EVENTS = (ADDRESS_EVENT, BOUNCE_EVENT, COMPLAINT_EVENT)

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

    Shared flow references are retired. An unmigrated reference fails closed
    until the one-time migration embeds its actions in the trigger.
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
    """No shared flows are offered after the managed-store cutover."""
    from ..engine import matching

    return matching.flow_catalog()


def managed_routes():
    """Address routes claimed by published workflows, each with its owner.

    One entry per (route, workflow) pair: a workflow may claim several
    routes, and a route claimed by two workflows lists both owners. These
    render next to the stored triggers in the console and CLI — every
    address the domain answers is one row of the same list, never a
    footnote. ``status`` mirrors the owning workflow's run state.
    """
    from .. import engine

    routes = {}
    for workflow in engine.workflows():
        for trigger in engine.workflow_triggers(workflow):
            if trigger.get("connector") != "email":
                continue
            rule = (trigger.get("filters") or {}).get("route") or {}
            if not (isinstance(rule, dict) and rule.get("equals")):
                continue
            key = (str(rule["equals"]).lower(), str(workflow.get("id") or ""))
            if workflow.get("auto_paused") is True:
                status = "auto-paused"
            elif workflow.get("enabled", True):
                status = "enabled"
            else:
                status = "disabled"
            routes.setdefault(key, {"name": key[0], "workflow": key[1], "status": status})
    return [routes[key] for key in sorted(routes)]


def yaml_email_routes():
    """Routes already claimed by YAML workflows, so triggers cannot shadow them."""
    return {route["name"] for route in managed_routes()}


def build_item(body, operator):
    name = validate_name(body.get("name"))
    event = str(body.get("event") or ADDRESS_EVENT).strip()
    if event not in EVENTS:
        raise TriggerError(f"unknown email trigger event '{event}'; known: {', '.join(EVENTS)}")
    actions, flow = resolve_actions_flow(body)
    if event == ADDRESS_EVENT:
        # Only address triggers claim a route; watchers match SES feedback
        # for the whole domain and never shadow a YAML workflow's address.
        if name in yaml_email_routes():
            raise TriggerError(f"the route '{name}' is already handled by a YAML workflow")
        extra = body.get("filters") or {}
        if extra is None:
            extra = {}
        if not isinstance(extra, dict):
            raise TriggerError("filters must be an object")
        if "from" in extra:
            raise TriggerError("from is the shared sender list, not an address filter")
        for field, rule in extra.items():
            if field not in ("subject", "body"):
                raise TriggerError("address filters may only use subject or body")
            if not isinstance(rule, dict) or not rule:
                raise TriggerError(f"filter '{field}' must be an operator object")
            from ..engine.matching import _matches_filter
            try:
                _matches_filter("", rule)
            except KeyError:
                raise TriggerError(f"unknown operator in filter '{field}'") from None
        address, filters = address_for(name), extra
    else:
        # A route filter can never match a bounce/complaint (their data has
        # no route), so storing one would silently dead the trigger.
        filters = body.get("filters")
        if filters is None:
            filters = {}
        if not isinstance(filters, dict):
            raise TriggerError("filters must be an object")
        if "route" in filters:
            raise TriggerError("watchers match SES feedback, not addresses: filters cannot use 'route'")
        address = ""
    now = datetime.now(timezone.utc).isoformat()
    item = {
        "name": name,
        "address": address,
        "description": str(body.get("description") or "")[:200],
        "actions": actions or [],
        "flow": flow,
        "enabled": bool(body.get("enabled", True)),
        "created_by": str(operator or ""),
        "created_at": now,
        "updated_at": now,
    }
    # Legacy (event-absent) items stay byte-identical: the default event and
    # the watcher-only fields are stored only when a watcher is defined.
    if event != ADDRESS_EVENT:
        item["event"] = event
        item["filters"] = filters
    elif filters:
        item["filters"] = filters
    return item


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


def _scan_all(table):
    """Read every scan page: a single Limit=200 scan silently dropped
    trigger #201 and beyond — it would stop firing with no error anywhere.
    Same walk as published_workflows._scan_all (the managed-store loader)."""
    items, start = [], None
    while True:
        kwargs = {"Limit": SCAN_LIMIT, "ConsistentRead": True}
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
            "event": str(item.get("event") or ADDRESS_EVENT),
            # Watchers keep their stored filters (default match-all);
            # address triggers scope to the reserved route as before.
            "filters": (
                {"route": {"equals": item["name"]}, **dict(item.get("filters") or {})}
                if not item.get("event") or item["event"] == ADDRESS_EVENT
                else dict(item.get("filters") or {})
            ),
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
    view = {key: item.get(key) for key in (
        "name", "address", "description", "actions", "flow", "enabled",
        "created_by", "created_at", "updated_at",
    )}
    view["event"] = item.get("event") or ADDRESS_EVENT
    if item.get("filters") or (item.get("event") and item["event"] != ADDRESS_EVENT):
        view["filters"] = item.get("filters") or {}
    return view
