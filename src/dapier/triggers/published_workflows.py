"""Published workflows: the runtime store behind instant designer saves.

Saving a workflow commits its YAML to git (the version record) and puts the
parsed definition into PUBLISHED_WORKFLOWS_TABLE. The engine merges published
items over the deployed bundle by workflow id, so a save takes effect on the
next event — no deploy. The enable/disable toggle updates the stored item the
same way, then commits the flipped YAML so the next deploy cannot revert it.

Items are keyed by workflow_id; a rename must unpublish the old id or the
engine would run both the old and the new definition.
"""

import os
from datetime import date, datetime, timezone
from decimal import Decimal

TABLE_ENV = "PUBLISHED_WORKFLOWS_TABLE"
SCAN_LIMIT = 200


class PublishError(Exception):
    """The publish table is not configured (or unusable) in this deployment."""


def configured():
    return bool(os.environ.get(TABLE_ENV))


def get_table(table_ref=None):
    if table_ref is not None:
        return table_ref
    name = os.environ.get(TABLE_ENV)
    if not name:
        raise PublishError("published workflows are not configured")
    import boto3

    return boto3.resource("dynamodb").Table(name)


def _scrub(value):
    """DynamoDB rejects None and date types; workflow YAML can contain both."""
    if isinstance(value, dict):
        return {key: _scrub(item) for key, item in value.items() if item is not None}
    if isinstance(value, (list, tuple)):
        return [_scrub(item) for item in value if item is not None]
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    return value


def _decode_numbers(value):
    """The resource API returns Decimals; the engine needs JSON-safe values."""
    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral_value() else float(value)
    if isinstance(value, dict):
        return {key: _decode_numbers(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_decode_numbers(item) for item in value]
    return value


def publish(workflow, *, operator=None, previous=None, table_ref=None):
    """Store one parsed workflow definition; the engine reads it on the next event."""
    now = datetime.now(timezone.utc).isoformat()
    previous = previous or {}
    item = {
        "workflow_id": workflow["id"],
        "file": f"{workflow['id']}.yaml",
        "workflow": _scrub(workflow),
        "enabled": bool(workflow.get("enabled", True)),
        "published_by": previous.get("published_by") or str(operator or ""),
        "created_at": previous.get("created_at") or now,
        "updated_at": now,
    }
    get_table(table_ref).put_item(Item=item)
    return item


def unpublish(workflow_id, table_ref=None):
    get_table(table_ref).delete_item(Key={"workflow_id": workflow_id})


def get_item(workflow_id, table_ref=None):
    item = get_table(table_ref).get_item(Key={"workflow_id": workflow_id}).get("Item")
    return {key: _decode_numbers(value) for key, value in item.items()} if item else None


def load_items(table_ref=None):
    items = get_table(table_ref).scan(Limit=SCAN_LIMIT).get("Items", [])
    return sorted(
        ({key: _decode_numbers(value) for key, value in item.items()} for item in items),
        key=lambda item: item.get("workflow_id", ""),
    )


def load_workflows(table_ref=None):
    """Parsed definitions from the table, ready for the engine's merge."""
    return [
        item["workflow"] for item in load_items(table_ref=table_ref)
        if isinstance(item.get("workflow"), dict) and item["workflow"].get("id")
    ]
