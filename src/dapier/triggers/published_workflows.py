"""Managed workflows: the runtime store behind instant designer saves.

Saving a workflow puts its parsed definition into PUBLISHED_WORKFLOWS_TABLE;
the next event reads it without a deploy. Git sync is an optional copy. The
enable/disable toggle updates the same managed record.

Items are keyed by workflow_id; a rename must unpublish the old id or the
engine would run both the old and the new definition.

Every publish also appends a version record under the key ``<id>#v<n>`` (same
table, ``version_of`` attribute set) — Zapier's version history: who published
what, when, and why (save, toggle, rollback). The live item carries the
monotonic ``revision``. Version records are filtered out of every engine/
designer read and kept per workflow up to MAX_VERSIONS.
"""

import os
from datetime import date, datetime, timezone
from decimal import Decimal

TABLE_ENV = "PUBLISHED_WORKFLOWS_TABLE"
SCAN_LIMIT = 200
MAX_VERSIONS = 20


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


def publish(workflow, *, operator=None, previous=None, cause="save", table_ref=None,
            only_if_absent=False, expected_revision=None):
    """Store one parsed workflow definition; the engine reads it on the next event.

    Also appends a version record (the definition exactly as published, with
    its revision, operator, and cause) and prunes history beyond MAX_VERSIONS.
    """
    now = datetime.now(timezone.utc).isoformat()
    previous = previous or {}
    revision = int(previous.get("revision") or 0) + 1
    item = {
        "workflow_id": workflow["id"],
        "file": f"{workflow['id']}.yaml",
        "workflow": _scrub(workflow),
        "enabled": bool(workflow.get("enabled", True)),
        "published_by": str(operator or "") or previous.get("published_by") or "",
        "created_at": previous.get("created_at") or now,
        "updated_at": now,
        "revision": revision,
    }
    table = get_table(table_ref)
    write = {"Item": item}
    if only_if_absent:
        write["ConditionExpression"] = "attribute_not_exists(workflow_id)"
    elif expected_revision is not None:
        write["ConditionExpression"] = "revision = :expected"
        write["ExpressionAttributeValues"] = {":expected": int(expected_revision)}
    table.put_item(**write)
    table.put_item(Item={
        "workflow_id": version_key(workflow["id"], revision),
        "version_of": workflow["id"],
        "revision": revision,
        "workflow": item["workflow"],
        "enabled": item["enabled"],
        "published_by": str(operator or ""),
        "published_at": now,
        "cause": cause,
    })
    _prune_versions(workflow["id"], table)
    return item


def version_key(workflow_id, revision):
    return f"{workflow_id}#v{revision}"


def _prune_versions(workflow_id, table):
    versions = list_versions(workflow_id, table_ref=table)
    for version in versions[MAX_VERSIONS:]:
        table.delete_item(Key={"workflow_id": version["workflow_id"]})


def unpublish(workflow_id, table_ref=None):
    get_table(table_ref).delete_item(Key={"workflow_id": workflow_id})


def get_item(workflow_id, table_ref=None):
    item = get_table(table_ref).get_item(Key={"workflow_id": workflow_id}).get("Item")
    return {key: _decode_numbers(value) for key, value in item.items()} if item else None


def _scan_all(table):
    """Read every page; the managed table is the complete runtime catalog."""
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
    # Version records share the table; only live items belong in the
    # engine merge and the designer list.
    items = [
        item for item in _scan_all(get_table(table_ref))
        if not item.get("version_of")
    ]
    return sorted(
        ({key: _decode_numbers(value) for key, value in item.items()} for item in items),
        key=lambda item: item.get("workflow_id", ""),
    )


def list_versions(workflow_id, table_ref=None):
    """One workflow's version records, newest revision first.

    Version records share the table with live definitions, so all scan pages
    must be read before filtering this workflow's history.
    """
    versions = [
        item for item in _scan_all(get_table(table_ref))
        if item.get("version_of") == workflow_id
    ]
    return sorted(
        ({key: _decode_numbers(value) for key, value in item.items()} for item in versions),
        key=lambda item: int(item.get("revision") or 0),
        reverse=True,
    )


def get_version(workflow_id, revision, table_ref=None):
    item = get_table(table_ref).get_item(
        Key={"workflow_id": version_key(workflow_id, revision)},
    ).get("Item")
    return {key: _decode_numbers(value) for key, value in item.items()} if item else None


def load_workflows(table_ref=None):
    """Parsed definitions from the table, ready for the engine's merge."""
    return [
        item["workflow"] for item in load_items(table_ref=table_ref)
        if isinstance(item.get("workflow"), dict) and item["workflow"].get("id")
    ]
