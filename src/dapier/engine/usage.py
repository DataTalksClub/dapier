"""Task usage metering: one rollup item per workflow per month.

The worker's completion path ADDs to the month's item (best-effort — usage
must never break a run); the /api/{admin,agent}/usage endpoints read the
same items back as per-workflow-per-month task counts.
"""
import os
from datetime import datetime, timezone


def _table():
    import boto3

    return boto3.resource("dynamodb").Table(os.environ["TASK_USAGE_TABLE"])


def _month_key(year, month):
    return f"{year:04d}{month:02d}"


def add_task(workflow_id, now=None):
    """Count one completed action step for its month."""
    now = now or datetime.now(timezone.utc)
    _table().update_item(
        Key={"month": _month_key(now.year, now.month), "workflow_id": workflow_id},
        UpdateExpression="ADD #tasks :one",
        # ``tasks`` is mapped: bare attribute names are at the mercy of
        # DynamoDB's reserved-word list.
        ExpressionAttributeNames={"#tasks": "tasks"},
        ExpressionAttributeValues={":one": 1},
    )


def api_usage(months=12, now=None):
    """Per-workflow-per-month task counts for the last ``months`` months."""
    from boto3.dynamodb.conditions import Key

    try:
        months = max(1, min(int(months), 24))
    except (TypeError, ValueError):
        months = 12
    now = now or datetime.now(timezone.utc)
    year, month = now.year, now.month
    usage = []
    for _ in range(months):
        items = _table().query(
            KeyConditionExpression=Key("month").eq(_month_key(year, month)),
        ).get("Items", [])
        for item in items:
            usage.append({
                "month": item.get("month"),
                "workflow_id": item.get("workflow_id"),
                "tasks": int(item.get("tasks") or 0),
            })
        month -= 1
        if month == 0:
            month, year = 12, year - 1
    usage.sort(key=lambda row: (str(row["month"]), str(row["workflow_id"])),
               reverse=True)
    return 200, {"usage": usage}
