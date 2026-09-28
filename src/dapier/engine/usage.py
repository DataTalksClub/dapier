"""Task usage metering and the monthly task quota: the Zapier plan limit.

The worker's completion path ADDs to the month's item (best-effort — usage
must never break a run); the /api/{admin,agent}/usage endpoints read the
same items back as per-workflow-per-month task counts.

On top of metering sits the quota: one account-wide monthly task budget
stored in the same table as a ``quota`` config item (absent = uncapped).
``enforce`` is the worker's gate — before an action step runs it checks the
month's ``_total`` rollup against the limit and raises
``logic.QuotaExceeded`` when the budget is spent, which fails the step
through the ordinary action-error machinery. Metering and gating are both
best-effort: an infra failure opens the gate rather than failing the run.
"""
import logging
import os
from datetime import datetime, timezone

from .logic import QuotaExceeded

logger = logging.getLogger(__name__)

# Account-wide rollup row (one month partition) and the quota config item —
# both live in TaskUsageTable next to the per-workflow rows; api_usage
# filters them out so callers only ever see real workflows.
TOTAL_WORKFLOW_ID = "_total"
QUOTA_MONTH = "quota"
QUOTA_WORKFLOW_ID = "task-quota"


def _table():
    import boto3

    return boto3.resource("dynamodb").Table(os.environ["TASK_USAGE_TABLE"])


def _month_key(year, month):
    return f"{year:04d}{month:02d}"


def add_task(workflow_id, now=None):
    """Count one completed action step for its month: the per-workflow row
    plus the account-wide ``_total`` row the quota gate reads."""
    now = now or datetime.now(timezone.utc)
    table = _table()
    for key_id in (workflow_id, TOTAL_WORKFLOW_ID):
        table.update_item(
            Key={"month": _month_key(now.year, now.month), "workflow_id": key_id},
            UpdateExpression="ADD #tasks :one",
            # ``tasks`` is mapped: bare attribute names are at the mercy of
            # DynamoDB's reserved-word list.
            ExpressionAttributeNames={"#tasks": "tasks"},
            ExpressionAttributeValues={":one": 1},
        )


def api_usage(months=12, now=None, visible=None):
    """Per-workflow-per-month task counts for the last ``months`` months.

    ``visible`` (an auth.visibility.Visibility, None = unrestricted)
    read-filters the rollup, G17 Phase 2: a non-operator keeps the rows of
    workflows it owns; a row whose workflow no longer exists resolves to no
    owner and stays visible. The account-wide ``_total`` row is excluded
    here either way (it is not a workflow), and the quota block is
    account-wide by design — no per-owner cut.
    """
    from boto3.dynamodb.conditions import Key

    try:
        months = max(1, min(int(months), 24))
    except (TypeError, ValueError):
        months = 12
    now = now or datetime.now(timezone.utc)
    year, month = now.year, now.month
    owners = _visible_owners(visible)
    usage = []
    for _ in range(months):
        items = _table().query(
            KeyConditionExpression=Key("month").eq(_month_key(year, month)),
        ).get("Items", [])
        for item in items:
            if item.get("workflow_id") == TOTAL_WORKFLOW_ID:
                continue
            usage.append({
                "month": item.get("month"),
                "workflow_id": item.get("workflow_id"),
                "tasks": int(item.get("tasks") or 0),
            })
        month -= 1
        if month == 0:
            month, year = 12, year - 1
    if visible is not None:
        usage = [row for row in usage
                 if visible.workflow_visible(row["workflow_id"], owners)]
    usage.sort(key=lambda row: (str(row["month"]), str(row["workflow_id"])),
               reverse=True)
    return 200, {"usage": usage}


def _visible_owners(visible):
    """The workflow-owner map a filtered usage read resolves rows against —
    skipped entirely for an unrestricted (operator) caller. Read filtering
    only: the metering and quota paths above never filter."""
    from ..auth import visibility

    if visible is None or visible.is_operator:
        return {}
    return visibility.workflow_owners()


def quota_status(now=None):
    """The current month's budget: limit, tasks used, tasks left.

    ``enabled`` is False (and ``limit``/``remaining`` None) while no limit
    is stored — metering alone, the pre-quota behavior.
    """
    now = now or datetime.now(timezone.utc)
    status = {
        "month": _month_key(now.year, now.month),
        "limit": None,
        "used": 0,
        "remaining": None,
        "enabled": False,
    }
    if not os.environ.get("TASK_USAGE_TABLE"):
        return status
    table = _table()
    config = table.get_item(
        Key={"month": QUOTA_MONTH, "workflow_id": QUOTA_WORKFLOW_ID},
    ).get("Item") or {}
    limit = config.get("limit")
    if limit:
        limit = int(limit)
        status["limit"] = limit
        status["enabled"] = True
    total = table.get_item(
        Key={"month": status["month"], "workflow_id": TOTAL_WORKFLOW_ID},
    ).get("Item") or {}
    used = int(total.get("tasks") or 0)
    status["used"] = used
    if status["enabled"]:
        status["remaining"] = max(0, limit - used)
    return status


def check_quota(now=None):
    """``(allowed, status)`` — True while the month's budget has room."""
    status = quota_status(now)
    if not status["enabled"]:
        return True, status
    return status["used"] < status["limit"], status


def enforce(workflow_id, now=None):
    """The worker's gate: raise ``QuotaExceeded`` once the budget is spent.

    Best-effort in both directions — with no usage table wired there is
    nothing to enforce against, and an infra failure opens the gate (usage
    must never break a run) rather than failing the step.
    """
    if not os.environ.get("TASK_USAGE_TABLE"):
        return
    try:
        allowed, status = check_quota(now)
    except Exception:
        logger.warning("task quota check failed; allowing the step", exc_info=True)
        return
    if not allowed:
        raise QuotaExceeded(
            f"task quota exceeded for {status['month']}: "
            f"{status['used']}/{status['limit']} tasks used — "
            "raise the limit with `dapier quota set` (or in the console)"
        )


def _write_limit(table, limit):
    if limit is None:
        table.delete_item(
            Key={"month": QUOTA_MONTH, "workflow_id": QUOTA_WORKFLOW_ID})
    else:
        table.put_item(Item={
            "month": QUOTA_MONTH, "workflow_id": QUOTA_WORKFLOW_ID,
            "limit": limit,
        })


def parse_limit(value):
    """The API body / CLI value into a stored limit: a positive int, or
    None for ``off``/``null``/``""`` (no cap). Raises ValueError otherwise —
    ``0`` is rejected on purpose: a zero budget reads like a mis-type, and
    disabling the cap is spelled ``off``."""
    if value is None:
        return None
    if isinstance(value, str) and value.strip().lower() in ("", "off", "none", "null"):
        return None
    if isinstance(value, bool):
        raise ValueError("limit must be a positive integer or 'off'")
    try:
        limit = int(value)
    except (TypeError, ValueError):
        raise ValueError("limit must be a positive integer or 'off'") from None
    if limit <= 0:
        raise ValueError("limit must be a positive integer or 'off'")
    return limit


def api_quota_get():
    """The quota block for both APIs: budget and this month's standing."""
    return 200, {"quota": quota_status()}


def api_quota_set(value, now=None):
    """Store or clear the monthly task budget; returns the new status."""
    try:
        limit = parse_limit(value)
    except ValueError as exc:
        return 400, {"error": str(exc)}
    if os.environ.get("TASK_USAGE_TABLE"):
        _write_limit(_table(), limit)
    return 200, {"quota": quota_status(now)}
