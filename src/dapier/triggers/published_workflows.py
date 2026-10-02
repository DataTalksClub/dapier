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

A designer save writes a *draft*, not a publish: one ``<id>#draft`` item per
workflow (``draft_of`` attribute set, carrying the drafted definition and the
live ``base_revision`` it was edited against, last write wins). Drafts share
the table but load_items drops them, so the engine and every list stay
draft-blind — a draft-only workflow fires nothing until it is published.
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


def resolve_owner(item):
    """The owner a reader sees: the stamped ``owner``, else the item's
    informational ``published_by`` (every item predating G17 has one), else
    "" — the Phase 1 backfill, applied on read so no migration is needed."""
    return str(item.get("owner") or item.get("published_by") or "")


def _decode_item(item):
    """Decode one stored item for readers: Decimals to JSON-safe numbers and
    ``owner`` resolved (see resolve_owner) — every live-item read answers
    with an owner, backfilled from published_by when the stamp predates it."""
    decoded = {key: _decode_numbers(value) for key, value in item.items()}
    decoded["owner"] = resolve_owner(decoded)
    return decoded


def publish(workflow, *, operator=None, previous=None, cause="save", table_ref=None,
            only_if_absent=False, expected_revision=None):
    """Store one parsed workflow definition; the engine reads it on the next event.

    Also appends a version record (the definition exactly as published, with
    its revision, operator, and cause) and prunes history beyond MAX_VERSIONS.
    The item is stamped with ``owner`` — the publishing operator's subject,
    preserved from the previous revision when the operator is unknown and
    falling back to the previous item's ``published_by`` (G17 Phase 1:
    informational for now; Phase 2 filters reads by it). Version records
    carry the same owner for the same reason they carry published_by.
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
        "owner": str(operator or "") or previous.get("owner")
                 or previous.get("published_by") or "",
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
        "owner": str(operator or ""),
        "published_at": now,
        "cause": cause,
    })
    _prune_versions(workflow["id"], table)
    return item


def version_key(workflow_id, revision):
    return f"{workflow_id}#v{revision}"


def draft_key(workflow_id):
    return f"{workflow_id}#draft"


def save_draft(workflow, *, base_revision, operator=None, rename_from=None,
               table_ref=None):
    """Store (or overwrite — last write wins) one workflow's draft definition.

    The draft is keyed ``<id>#draft`` and carries ``draft_of`` (the live id),
    the drafted definition, and ``base_revision`` — the live revision the
    edit was made against (0 when the workflow has never been published).
    Publishing compares it against the live revision to refuse promoting a
    draft that a toggle/rollback/auto-pause has raced past. ``rename_from``
    records an id rename made while drafting (a draft save never unpublishes
    the old id live — that happens when the draft is promoted).
    """
    now = datetime.now(timezone.utc).isoformat()
    item = {
        "workflow_id": draft_key(workflow["id"]),
        "draft_of": workflow["id"],
        "file": f"{workflow['id']}.yaml",
        "workflow": _scrub(workflow),
        "enabled": bool(workflow.get("enabled", True)),
        "base_revision": int(base_revision or 0),
        "drafted_by": str(operator or ""),
        "updated_at": now,
    }
    if rename_from and rename_from != item["file"]:
        item["rename_from"] = rename_from
    get_table(table_ref).put_item(Item=item)
    return item


def get_draft(workflow_id, table_ref=None):
    """The workflow's draft item, or None when nothing is drafted."""
    item = get_table(table_ref).get_item(
        Key={"workflow_id": draft_key(workflow_id)},
    ).get("Item")
    return {key: _decode_numbers(value) for key, value in item.items()} if item else None


def delete_draft(workflow_id, table_ref=None):
    get_table(table_ref).delete_item(Key={"workflow_id": draft_key(workflow_id)})


def _prune_versions(workflow_id, table):
    versions = list_versions(workflow_id, table_ref=table)
    for version in versions[MAX_VERSIONS:]:
        table.delete_item(Key={"workflow_id": version["workflow_id"]})


def unpublish(workflow_id, table_ref=None):
    get_table(table_ref).delete_item(Key={"workflow_id": workflow_id})


def get_item(workflow_id, table_ref=None):
    item = get_table(table_ref).get_item(Key={"workflow_id": workflow_id}).get("Item")
    return _decode_item(item) if item else None


def _scan_all(table):
    """Read every page; the managed table is the complete runtime catalog."""
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


def load_items(table_ref=None, include_drafts=False):
    # Version records and draft records share the table; only live items
    # belong in the engine merge and the designer list — a draft must never
    # fire or appear as live state. Callers that surface drafts (the list's
    # draft-only rows, the draft endpoints) ask for them explicitly.
    items = [
        item for item in _scan_all(get_table(table_ref))
        if not item.get("version_of")
        and (include_drafts or not item.get("draft_of"))
    ]
    return sorted(
        (_decode_item(item) for item in items),
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


def diff_versions(workflow_id, from_revision, to_revision, table_ref=None):
    """Two version records of one workflow side by side, ready to compare:
    ``{"from": <record>, "to": <record>}``. None when either revision is
    missing — never published, pruned beyond MAX_VERSIONS, or not a real
    number — so the caller answers 404 instead of half a diff."""
    source = get_version(workflow_id, from_revision, table_ref)
    target = get_version(workflow_id, to_revision, table_ref)
    if not source or not target:
        return None
    return {"from": source, "to": target}


def load_workflows(table_ref=None):
    """Parsed definitions from the table, ready for the engine's merge."""
    return [
        item["workflow"] for item in load_items(table_ref=table_ref)
        if isinstance(item.get("workflow"), dict) and item["workflow"].get("id")
    ]
