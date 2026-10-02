"""The bookkeeping review queue: parsed invoices pending human confirmation.

Every parsed invoice — deterministic or AI — lands here as a *pending*
entry and nothing else happens with it until a reviewer confirms or
rejects it (the review endpoints). The ledger-facing consumers read only
confirmed entries, so a bad extraction can never reach a report.

The table (``BOOKKEEPING_TABLE``) holds two item kinds:

- the entry itself: partition ``entry``, sort ``entry_id``;
- one dedup marker per provider+invoice number: partition ``dedup``,
  sort ``<provider>/<invoice_number>``. ``create_pending`` claims it with a
  conditional put, so an SQS redelivery of the same email stages one entry
  and reports ``deduped`` instead of duplicating. Rejecting an entry drops
  its marker, so a corrected re-send can stage a fresh entry.

Amounts and VAT are stored as Decimals (DynamoDB has no float type) and
decoded back to floats on every read.
"""

import os
import re
from datetime import datetime, timezone
from decimal import Decimal

TABLE_ENV = "BOOKKEEPING_TABLE"

ENTRY_PARTITION = "entry"
DEDUP_PARTITION = "dedup"

STATUS_PENDING = "pending"
STATUS_CONFIRMED = "confirmed"
STATUS_REJECTED = "rejected"
STATUSES = (STATUS_PENDING, STATUS_CONFIRMED, STATUS_REJECTED)

# The entry fields a reviewer may correct. Everything else (ids, timestamps,
# provenance) is system-owned.
EDITABLE_FIELDS = frozenset({
    "provider", "what", "invoice_number", "date_issued", "date_paid",
    "amount", "currency", "vat", "period", "period_month", "account",
})

LIST_DEFAULT_LIMIT = 50
LIST_MAX_LIMIT = 200


class BookkeepingError(ValueError):
    """The queue is not configured, or the request names a missing entry."""


def _table():
    name = str(os.environ.get(TABLE_ENV) or "").strip()
    if not name:
        raise BookkeepingError(
            f"the bookkeeping queue is not configured (set the {TABLE_ENV} "
            "environment variable on this function)")
    import boto3

    return boto3.resource("dynamodb").Table(name)


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _decimals(value):
    """Floats become Decimals (DynamoDB's only number type); recursively."""
    if isinstance(value, bool) or value is None:
        return value
    if isinstance(value, float):
        return Decimal(str(value))
    if isinstance(value, dict):
        return {key: _decimals(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_decimals(item) for item in value]
    return value


def _decode(value):
    """Decimals and DynamoDB's other non-JSON types become plain values."""
    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral_value() else float(value)
    if isinstance(value, dict):
        return {key: _decode(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_decode(item) for item in value]
    return value


def dedup_key_for(entry):
    """The natural identity of an invoice: provider + invoice number."""
    provider = str((entry or {}).get("provider") or "").strip().lower()
    number = str((entry or {}).get("invoice_number") or "").strip().lower()
    if not (provider and number):
        return None
    return f"{provider}/{re.sub(r'\s+', ' ', number)}"


def _entry_item(entry_id, *, entry, source, workflow_id, message, pdf, now):
    clean_entry = {key: value for key, value in (entry or {}).items() if value is not None}
    item = {
        "pk": ENTRY_PARTITION,
        "sk": entry_id,
        "entry_id": entry_id,
        "status": STATUS_PENDING,
        "source": str(source or ""),
        "workflow_id": str(workflow_id or ""),
        "created_at": now,
        "entry": _decimals(clean_entry),
    }
    if message:
        item["message"] = _decimals(message)
    if pdf:
        item["pdf"] = _decimals(pdf)
    return item


def create_pending(*, entry, source, workflow_id, message=None, pdf=None,
                   table=None, now=None):
    """Stage one parsed invoice as a pending review entry.

    Returns ``{entry_id, status, deduped}``; a redelivery of an invoice that
    already has a pending or confirmed entry returns the original ``entry_id``
    with ``deduped: true`` and stages nothing."""
    table = table if table is not None else _table()
    now = now or _now()
    dedup_key = dedup_key_for(entry)
    if dedup_key:
        existing = _dedup_target(table, dedup_key)
        if existing:
            return {"entry_id": existing, "status": _entry_status(table, existing),
                    "deduped": True}
    entry_id = _new_entry_id()
    if dedup_key:
        try:
            table.put_item(
                Item={"pk": DEDUP_PARTITION, "sk": dedup_key, "entry_id": entry_id},
                ConditionExpression="attribute_not_exists(sk)")
        except table.meta.client.exceptions.ConditionalCheckFailedException:
            existing = _dedup_target(table, dedup_key)
            if existing:
                return {"entry_id": existing, "status": _entry_status(table, existing),
                        "deduped": True}
            raise
    table.put_item(Item=_entry_item(
        entry_id, entry=entry, source=source, workflow_id=workflow_id,
        message=message, pdf=pdf, now=now))
    return {"entry_id": entry_id, "status": STATUS_PENDING, "deduped": False}


def _new_entry_id():
    import uuid

    return uuid.uuid4().hex


def _dedup_target(table, dedup_key):
    """The entry id the dedup marker points at, or None — a marker whose
    entry vanished (a crashed stage between the two puts) is stale and
    ignored, so the caller recreates both."""
    item = table.get_item(Key={"pk": DEDUP_PARTITION, "sk": dedup_key}).get("Item")
    if not item:
        return None
    entry_id = str(item.get("entry_id") or "")
    if entry_id and _entry_status(table, entry_id) is not None:
        return entry_id
    table.delete_item(Key={"pk": DEDUP_PARTITION, "sk": dedup_key})
    return None


def _entry_status(table, entry_id):
    item = table.get_item(Key={"pk": ENTRY_PARTITION, "sk": entry_id}).get("Item")
    return item.get("status") if item else None


def get_entry(entry_id, table=None):
    """One entry by id, or None."""
    table = table if table is not None else _table()
    item = table.get_item(Key={"pk": ENTRY_PARTITION, "sk": entry_id}).get("Item")
    return _decode(item) if item else None


def list_entries(status=None, limit=None, table=None):
    """Entries, newest first — the whole queue, or one status's slice.

    The queue is small (a few invoices a month), so a filtered scan is the
    honest read; the status filter runs server-side."""
    table = table if table is not None else _table()
    try:
        limit = min(max(int(limit or LIST_DEFAULT_LIMIT), 1), LIST_MAX_LIMIT)
    except (TypeError, ValueError):
        limit = LIST_DEFAULT_LIMIT
    expression = "#pk = :pk"
    names = {"#pk": "pk"}
    values = {":pk": ENTRY_PARTITION}
    if status:
        if status not in STATUSES:
            raise BookkeepingError(
                f"unknown status {status!r} (one of {', '.join(STATUSES)})")
        expression += " AND #st = :st"
        names["#st"] = "status"
        values[":st"] = status
    items = []
    scan = table.scan(
        FilterExpression=expression, ExpressionAttributeNames=names,
        ExpressionAttributeValues=values, Limit=200)
    items.extend(scan.get("Items", []))
    while scan.get("LastEvaluatedKey") and len(items) < LIST_MAX_LIMIT:
        scan = table.scan(
            FilterExpression=expression, ExpressionAttributeNames=names,
            ExpressionAttributeValues=values, Limit=200,
            ExclusiveStartKey=scan["LastEvaluatedKey"])
        items.extend(scan.get("Items", []))
    items.sort(key=lambda item: str(item.get("created_at") or ""), reverse=True)
    return [_decode(item) for item in items[:limit]]


def confirm_entry(entry_id, *, edits=None, confirmed_by="", table=None):
    """Confirm a pending entry, optionally applying the reviewer's field
    corrections first. Confirmed entries are final — confirm is the write
    that lets the entry reach the ledger; corrections ride the confirm."""
    table = table if table is not None else _table()
    item = table.get_item(Key={"pk": ENTRY_PARTITION, "sk": entry_id}).get("Item")
    if not item:
        raise BookkeepingError(f"no bookkeeping entry {entry_id}")
    if item.get("status") != STATUS_PENDING:
        raise BookkeepingError(
            f"entry {entry_id} is {item.get('status')}, only pending entries "
            "can be confirmed")
    entry = dict(_decode(item.get("entry") or {}))
    for key, value in (edits or {}).items():
        if key not in EDITABLE_FIELDS:
            raise BookkeepingError(f"field {key!r} is not a correctable entry field")
        if value is None:
            entry.pop(key, None)
        else:
            entry[key] = _decode(_decimals(value))
    table.update_item(
        Key={"pk": ENTRY_PARTITION, "sk": entry_id},
        UpdateExpression="SET #st = :st, entry = :entry, confirmed_at = :at, confirmed_by = :by",
        ExpressionAttributeNames={"#st": "status"},
        ExpressionAttributeValues={
            ":st": STATUS_CONFIRMED,
            ":entry": _decimals(entry),
            ":at": _now(),
            ":by": str(confirmed_by or ""),
        })
    return get_entry(entry_id, table=table)


def reject_entry(entry_id, *, rejected_by="", note=None, table=None):
    """Reject a pending entry. The dedup marker goes with it: a corrected
    re-send of the same invoice stages a fresh entry instead of deduping
    into the rejected one."""
    table = table if table is not None else _table()
    item = table.get_item(Key={"pk": ENTRY_PARTITION, "sk": entry_id}).get("Item")
    if not item:
        raise BookkeepingError(f"no bookkeeping entry {entry_id}")
    if item.get("status") != STATUS_PENDING:
        raise BookkeepingError(
            f"entry {entry_id} is {item.get('status')}, only pending entries "
            "can be rejected")
    dedup = dedup_key_for(_decode(item.get("entry") or {}))
    table.update_item(
        Key={"pk": ENTRY_PARTITION, "sk": entry_id},
        UpdateExpression="SET #st = :st, rejected_at = :at, rejected_by = :by"
                         + (" , reject_note = :note" if note else ""),
        ExpressionAttributeNames={"#st": "status"},
        ExpressionAttributeValues={
            ":st": STATUS_REJECTED,
            ":at": _now(),
            ":by": str(rejected_by or ""),
            **({":note": str(note)} if note else {}),
        })
    if dedup:
        table.delete_item(Key={"pk": DEDUP_PARTITION, "sk": dedup})
    return get_entry(entry_id, table=table)


def counts(table=None):
    """How many entries sit in each status — the review queue's headline."""
    table = table if table is not None else _table()
    tally = {status: 0 for status in STATUSES}
    scan = table.scan(
        FilterExpression="#pk = :pk",
        ExpressionAttributeNames={"#pk": "pk"},
        ExpressionAttributeValues={":pk": ENTRY_PARTITION},
        Select="COUNT")
    total = scan.get("Count", 0)
    while scan.get("LastEvaluatedKey"):
        scan = table.scan(
            FilterExpression="#pk = :pk",
            ExpressionAttributeNames={"#pk": "pk"},
            ExpressionAttributeValues={":pk": ENTRY_PARTITION},
            Select="COUNT", ExclusiveStartKey=scan["LastEvaluatedKey"])
        total += scan.get("Count", 0)
    for item in list_entries(table=table, limit=LIST_MAX_LIMIT):
        if item.get("status") in tally:
            tally[item["status"]] += 1
    tally["total"] = total
    return tally
