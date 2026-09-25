"""Cron and rate schedule triggers backed by EventBridge rules.

Each trigger owns one EventBridge rule named ``dapier-schedule-{name}``
whose schedule expression is a ``cron(...)`` or ``rate(...)`` string. The
rule's target is the worker function with a constant input payload naming
the trigger, so a fire is just a worker invocation: the worker turns it
into a normal event and the engine merges the stored actions from the
SCHEDULE_TRIGGERS_TABLE like any other trigger. Saves and deletes map
straight onto ``PutRule``/``PutTargets``/``DeleteRule``, so editing a
schedule reprograms the rule with no deploy.

The invoke permission for EventBridge on the worker is provisioned once
by the template for the ``dapier-schedule-*`` rule family; triggers only
ever call the Events API.
"""

import json
import logging
import os
import re
from datetime import datetime, timezone
from decimal import Decimal

from .email_triggers import (
    NAME_PATTERN, TriggerError, flow_catalog, resolve_actions_flow,
)

logger = logging.getLogger(__name__)

TABLE_ENV = "SCHEDULE_TRIGGERS_TABLE"
WORKER_ARN_ENV = "WORKER_FUNCTION_ARN"
RULE_PREFIX = "dapier-schedule-"
TARGET_ID = "dapier-worker"
SCHEDULE_EVENT = "schedule.triggered"

EXPRESSION_PATTERN = re.compile(
    r"^cron\(\s*\S+\s+\S+\s+\S+\s+\S+\s+\S+\s+\S+\s*\)$"
    r"|^rate\(\s*\d+\s+(minutes?|hours?|days?)\s*\)$"
)


def validate_expression(expression):
    expression = str(expression or "").strip()
    if not EXPRESSION_PATTERN.fullmatch(expression):
        raise TriggerError(
            "schedule expression must be cron(minute hour dom month dow year) "
            "or rate(<number> minutes|hours|days)")
    return expression


def rule_name(schedule_id):
    return f"{RULE_PREFIX}{schedule_id}"


def workflow_id_for(item):
    return f"schedule-trigger-{item['schedule_id']}"


def build_item(body, operator, previous=None):
    """Build the stored item for a create or edit."""
    if not isinstance(body, dict):
        raise TriggerError("request body must be an object")
    name = str(body.get("name") or "").strip().lower()
    if not NAME_PATTERN.fullmatch(name):
        raise TriggerError("schedule name must be 2-32 chars: lowercase letters, digits, hyphens")
    previous = previous or {}
    if previous and previous.get("schedule_id") != name:
        raise TriggerError(f"schedule id mismatch: stored as '{previous.get('schedule_id')}'")
    actions, flow = resolve_actions_flow(body)
    return {
        "schedule_id": name,
        "expression": validate_expression(body.get("expression")),
        "description": str(body.get("description") or "")[:200],
        "actions": actions or [],
        "flow": flow,
        "enabled": bool(body.get("enabled", True)),
        "created_by": previous.get("created_by") or str(operator or ""),
        "created_at": previous.get("created_at") or datetime.now(timezone.utc).isoformat(),
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }


def get_table(table_ref=None):
    if table_ref is not None:
        return table_ref
    name = os.environ.get(TABLE_ENV)
    if not name:
        raise TriggerError("schedule triggers are not configured")
    import boto3

    return boto3.resource("dynamodb").Table(name)


def worker_arn():
    arn = (os.environ.get(WORKER_ARN_ENV) or "").strip()
    if not arn:
        raise TriggerError("the worker function ARN is not configured for schedule triggers")
    return arn


def _events(events_client=None):
    if events_client is not None:
        return events_client
    import boto3

    return boto3.client("events")


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
        key=lambda item: item.get("schedule_id", ""),
    )


def get_item(name, table_ref=None):
    name = str(name or "").strip().lower()
    item = get_table(table_ref).get_item(Key={"schedule_id": name}).get("Item")
    return {key: _decode_numbers(value) for key, value in item.items()} if item else None


def sync_rule(item, *, events_client=None, target_arn=None):
    """Create or update the EventBridge rule and its worker target.

    ``PutRule`` with the same name reprograms the schedule in place; the
    constant target input is what tells the worker which trigger fired.
    """
    events = _events(events_client)
    rule = rule_name(item["schedule_id"])
    state = "ENABLED" if item.get("enabled", True) else "DISABLED"
    description = item.get("description") or "Dapier schedule trigger"
    events.put_rule(
        Name=rule,
        ScheduleExpression=item["expression"],
        State=state,
        Description=description,
    )
    events.put_targets(
        Rule=rule,
        Targets=[{
            "Id": TARGET_ID,
            "Arn": target_arn if target_arn is not None else worker_arn(),
            "Input": json.dumps({"trigger": "schedule", "schedule_id": item["schedule_id"]}),
        }],
    )


def remove_rule(schedule_id, *, events_client=None):
    """Best-effort teardown of the rule; a missing rule is already gone."""
    from botocore.exceptions import ClientError

    events = _events(events_client)
    rule = rule_name(schedule_id)
    try:
        events.remove_targets(Rule=rule, Ids=[TARGET_ID], Force=True)
        events.delete_rule(Name=rule, Force=True)
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") != "ResourceNotFoundException":
            raise


def workflow_for(item):
    """The engine workflow for a stored schedule, or None when its flow is gone."""
    actions = item.get("actions") or []
    flow = str(item.get("flow") or "").strip()
    if flow:
        from ..engine import matching

        actions = matching.flow_actions(flow)
        if actions is None:
            logger.warning("schedule trigger '%s' binds undefined flow '%s'; skipped",
                           item.get("schedule_id"), flow)
            return None
    return {
        "id": workflow_id_for(item),
        "enabled": True,
        "trigger": {
            "connector": "schedule",
            "event": SCHEDULE_EVENT,
            "filters": {"schedule": {"equals": item["schedule_id"]}},
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
    return {
        "schedule_id": item.get("schedule_id"),
        "expression": item.get("expression"),
        "rule": rule_name(item.get("schedule_id", "")),
        "description": item.get("description"),
        "actions": item.get("actions"),
        "flow": item.get("flow"),
        "enabled": item.get("enabled", True),
        "created_by": item.get("created_by"),
        "created_at": item.get("created_at"),
        "updated_at": item.get("updated_at"),
    }


def api_list(table_ref=None):
    return 200, {
        "schedules": [public_view(item) for item in load_items(table_ref=table_ref)],
        "flows": flow_catalog(),
    }


def api_save(body, operator, *, table_ref=None, events_client=None, target_arn=None):
    name = str((body or {}).get("name") or "").strip().lower()
    previous = get_item(name, table_ref=table_ref)
    item = build_item(body, operator, previous=previous)
    # Sync the rule before storing: a failed Events call must not leave a
    # stored trigger whose schedule never fires.
    sync_rule(item, events_client=events_client, target_arn=target_arn)
    get_table(table_ref).put_item(Item=item)
    return 200, {"created": previous is None, **public_view(item)}


def api_delete(name, operator, *, table_ref=None, events_client=None):
    name = str(name or "").strip().lower()
    item = get_item(name, table_ref=table_ref)
    if not item:
        raise TriggerError(f"no schedule trigger named '{name}'")
    remove_rule(item["schedule_id"], events_client=events_client)
    get_table(table_ref).delete_item(Key={"schedule_id": item["schedule_id"]})
    return 200, {"ok": True, "schedule_id": item["schedule_id"]}
