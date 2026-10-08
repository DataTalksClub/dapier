"""Cron and rate schedule triggers backed by EventBridge rules.

Each trigger owns one EventBridge rule named ``dapier-schedule-{name}``
whose schedule expression is a ``cron(...)`` or ``rate(...)`` string. The
rule's target is the worker function with a constant input payload naming
the trigger, so a fire is just a worker invocation: the worker turns it
into a normal ``schedule``/``schedule.triggered`` event and the engine
matches the designer workflows filtering on the ``schedule`` field. Saves
and deletes map
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
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from . import schedule_times
from .email_triggers import NAME_PATTERN, TriggerError

logger = logging.getLogger(__name__)

TABLE_ENV = "SCHEDULE_TRIGGERS_TABLE"
WORKER_ARN_ENV = "WORKER_FUNCTION_ARN"
# DynamoDB scan page size, not a cutoff: load_items walks pages to exhaustion.
SCAN_LIMIT = 200
RULE_PREFIX = "dapier-schedule-"
TARGET_ID = "dapier-worker"
SCHEDULE_EVENT = "schedule.triggered"

# Fire history kept on each schedule item, newest first: enough to see a
# schedule that stopped, small enough to stay far below the item limit.
FIRES_KEEP = 20
# Outcomes of one fire, and the two history markers a pause/resume leaves.
RAN = "ran"                    # at least one workflow matched and ran
NO_LISTENERS = "no_listeners"  # fired, but no published workflow filters on it
FAILED = "failed"              # the run failed (after any retries)
RETRYING = "retrying"          # failed; the workflow's retry policy re-queued it
PAUSED = "paused"
RESUMED = "resumed"
FIRE_OUTCOMES = (RAN, NO_LISTENERS, FAILED, RETRYING)
# EventBridge delivers within a minute or two; a fire this late is missed.
LATE_GRACE = timedelta(minutes=10)
UPCOMING_MAX_HOURS = 24 * 7
UPCOMING_LIMIT = 200
# A schedule firing hourly or more often is summarized in Upcoming
# ("every 5 minutes — 288 fires") instead of listed fire by fire.
FREQUENT_SECONDS = 3600

EXPRESSION_PATTERN = re.compile(
    # EventBridge cron's optional seventh field is an IANA timezone
    # (e.g. cron(0 9 ? * MON * Europe/Berlin)); it is passed through to
    # PutRule untouched, which rejects unsupported tz names itself.
    r"^cron\(\s*\S+\s+\S+\s+\S+\s+\S+\s+\S+\s+\S+(?:\s+\S+)?\s*\)$"
    r"|^rate\(\s*\d+\s+(minutes?|hours?|days?)\s*\)$"
)


def validate_expression(expression):
    expression = str(expression or "").strip()
    if not EXPRESSION_PATTERN.fullmatch(expression):
        raise TriggerError(
            "schedule expression must be cron(minute hour dom month dow year [timezone]) "
            "or rate(<number> minutes|hours|days)")
    return expression


def rule_name(schedule_id):
    return f"{RULE_PREFIX}{schedule_id}"


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
    now = datetime.now(timezone.utc).isoformat()
    item = {
        "schedule_id": name,
        "expression": validate_expression(body.get("expression")),
        "description": str(body.get("description") or "")[:200],
        "enabled": bool(body.get("enabled", True)),
        "created_by": previous.get("created_by") or str(operator or ""),
        "created_at": previous.get("created_at") or now,
        "updated_at": now,
    }
    # The fire history belongs to the schedule, not to one save of it.
    fires = list(previous.get("fires") or [])
    if previous and bool(previous.get("enabled", True)) != item["enabled"]:
        fires = _with_entry(fires, {
            "at": now, "outcome": RESUMED if item["enabled"] else PAUSED,
            "by": str(operator or "")})
    if fires:
        item["fires"] = fires
    return item


def _with_entry(fires, entry):
    """The history with ``entry`` on top: a re-recorded event id (a retry
    closing out) replaces its earlier row instead of adding a second one."""
    event_id = entry.get("event_id")
    kept = [fire for fire in fires
            if not (event_id and isinstance(fire, dict) and fire.get("event_id") == event_id)]
    return [entry, *kept][:FIRES_KEEP]


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


def _scan_all(table):
    """Read every scan page: a single Limit=200 scan silently dropped
    trigger #201 and beyond — it would stop firing with no error anywhere.
    Same walk as published_workflows._scan_all (the managed-store loader)."""
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
    return sorted(
        ({key: _decode_numbers(value) for key, value in item.items()}
         for item in _scan_all(get_table(table_ref))),
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


# --- fire history -------------------------------------------------------------

def fire_entry(event, outcome, workflows=None, error=None):
    """One history row for a fire (``event`` is the normalized envelope)."""
    data = (event or {}).get("data") or {}
    entry = {
        "at": (event or {}).get("occurred_at") or datetime.now(timezone.utc).isoformat(),
        "outcome": outcome,
        "event_id": str((event or {}).get("id") or ""),
        "workflows": [str(workflow) for workflow in (workflows or [])],
    }
    if data.get("manual"):
        entry["manual"] = True
        if data.get("requested_by"):
            entry["by"] = str(data["requested_by"])
    if error is not None:
        entry["error"] = str(error)[:300]
    return entry


def record_fire(event, outcome, workflows=None, error=None, *, table_ref=None):
    """Note one fire on its schedule's history. Best-effort like the inbox:
    a history write must never fail the fire itself, and a schedule deleted
    meanwhile is not re-created. Returns the entry, or None when skipped."""
    schedule_id = str(((event or {}).get("data") or {}).get("schedule") or "").strip().lower()
    if not schedule_id:
        return None
    try:
        table = get_table(table_ref)
        stored = table.get_item(Key={"schedule_id": schedule_id}).get("Item")
        if not stored:
            return None
        entry = fire_entry(event, outcome, workflows, error)
        stored["fires"] = _with_entry(_decode_numbers(stored.get("fires") or []), entry)
        table.put_item(Item=stored)
        return entry
    except Exception:
        logger.warning("schedule fire record failed", extra={"schedule_id": schedule_id})
        return None


# --- reading a schedule ----------------------------------------------------------

def fire_event(schedule_id, *, manual=False, requested_by=None, now=None):
    """The envelope one fire publishes (the worker mints scheduled ones the
    same way; ``manual`` marks a Run now)."""
    fired_at = (now or datetime.now(timezone.utc)).isoformat()
    data = {"schedule": schedule_id, "utc_time": fired_at}
    if manual:
        data["manual"] = True
        if requested_by:
            data["requested_by"] = str(requested_by)
    return {
        "schema_version": "1.0",
        "id": f"{schedule_id}-{'manual-' if manual else ''}{uuid.uuid4()}",
        "correlation_id": f"{schedule_id}-{fired_at}",
        "connector": "schedule",
        "event": SCHEDULE_EVENT,
        "source": schedule_id,
        "occurred_at": fired_at,
        "data": data,
    }


def load_workflows():
    """The published workflows, or [] when the store is unavailable: the
    listener column must never break the schedule list."""
    try:
        from ..engine.matching import workflows

        return workflows()
    except Exception:
        logger.warning("published workflows unavailable for schedule listeners")
        return []


def listeners(schedule_id, workflows):
    """The published workflows a fire of this schedule reaches.

    Matched with the engine's own rule, but with each workflow treated as
    on, so a paused workflow still shows (``enabled: false``) — it is the
    reason a fire runs nothing.
    """
    from ..engine.matching import matches

    event = {"connector": "schedule", "event": SCHEDULE_EVENT,
             "data": {"schedule": schedule_id, "utc_time": ""}}
    out = []
    for workflow in workflows or []:
        if not isinstance(workflow, dict) or not workflow.get("id"):
            continue
        if matches({**workflow, "enabled": True, "auto_paused": False}, event):
            out.append({
                "id": workflow["id"],
                "name": workflow.get("name") or workflow["id"],
                "enabled": bool(workflow.get("enabled", True)) and not workflow.get("auto_paused"),
            })
    return out


def _ago(moment, now):
    seconds = max(0, int((now - moment).total_seconds()))
    for unit, size in (("day", 86400), ("hour", 3600), ("minute", 60)):
        if seconds >= size:
            count = seconds // size
            return f"{count} {unit}{'s' if count != 1 else ''} ago"
    return "just now"


def _scheduled_fires(item):
    return [fire for fire in item.get("fires") or []
            if isinstance(fire, dict) and fire.get("outcome") in FIRE_OUTCOMES
            and not fire.get("manual")]


def _rate_anchor(item):
    """Where a rate schedule counts from: its last scheduled fire, else its
    last save (PutRule restarts the clock)."""
    fires = _scheduled_fires(item)
    return schedule_times.parse_time(fires[0]["at"] if fires else item.get("updated_at"))


def health(item, now, workflows):
    """``{"state", "reason"}`` — ok / waiting / attention / paused.

    Attention when the last scheduled fire failed, when an expected fire
    did not happen (walked forward from the last fire, or from the last
    save when nothing fired yet), or when fires reach no workflow.
    """
    expression = item.get("expression") or ""
    if not item.get("enabled", True):
        return {"state": "paused", "reason": "Paused. It does not fire until resumed."}
    fires = _scheduled_fires(item)
    last = fires[0] if fires else None
    every = schedule_times.describe_interval(schedule_times.typical_interval(expression, now))
    expected_text = f"expected every {every}" if every else "expected " + schedule_times.describe(expression)
    if last and last.get("outcome") == FAILED:
        reason = "The last fire failed"
        if last.get("error"):
            reason += f": {last['error']}"
        return {"state": "attention", "reason": reason}
    last_at = schedule_times.parse_time(last["at"]) if last else None
    saved_at = schedule_times.parse_time(item.get("updated_at"))
    since = max([moment for moment in (last_at, saved_at) if moment] or [now])
    due = schedule_times.next_fires(expression, since, count=1, until=now,
                                    anchor=_rate_anchor(item))
    if due and due[0] + LATE_GRACE < now:
        if last_at:
            reason = f"Last fired {_ago(last_at, now)}; {expected_text}."
        else:
            reason = f"No fire since it was saved {_ago(saved_at or now, now)}; {expected_text}."
        return {"state": "attention", "reason": reason}
    if not any(workflow.get("enabled") for workflow in workflows):
        name = item.get("schedule_id")
        if workflows:
            return {"state": "attention",
                    "reason": "Every workflow listening to it is turned off, so its fires run nothing."}
        return {"state": "attention",
                "reason": f"No published workflow filters on schedule = {name}, "
                          "so its fires run nothing."}
    if not last_at:
        return {"state": "waiting", "reason": "Waiting for its first fire."}
    return {"state": "ok", "reason": f"Last fired {_ago(last_at, now)}, on time."}


def _iso(moment):
    return moment.isoformat().replace("+00:00", "Z")


def public_view(item, *, now=None, workflows=None):
    """The schedule as the API shows it. With ``now`` the view carries the
    reading the Schedules tab and CLI need: plain language, next fires,
    the fire history, health, and the workflows it reaches."""
    view = {
        "schedule_id": item.get("schedule_id"),
        "expression": item.get("expression"),
        "rule": rule_name(item.get("schedule_id", "")),
        "description": item.get("description"),
        "enabled": item.get("enabled", True),
        "created_by": item.get("created_by"),
        "created_at": item.get("created_at"),
        "updated_at": item.get("updated_at"),
    }
    if now is None:
        return view
    expression = item.get("expression") or ""
    reached = listeners(item.get("schedule_id"), workflows or [])
    nexts = (schedule_times.next_fires(expression, now, count=5, anchor=_rate_anchor(item))
             if view["enabled"] else [])
    fires = [dict(fire) for fire in item.get("fires") or [] if isinstance(fire, dict)]
    for fire in fires:
        if fire.get("event_id") and fire.get("outcome") in FIRE_OUTCOMES:
            fire["runs"] = [{"workflow_id": workflow, "run_id": f"{workflow}:{fire['event_id']}"}
                            for workflow in fire.get("workflows") or []]
    # The last *scheduled* fire: a Run now says nothing about the rule.
    last = next((fire for fire in fires
                 if fire.get("outcome") in FIRE_OUTCOMES and not fire.get("manual")), None)
    view.update({
        "summary": schedule_times.describe(expression),
        "approximate": schedule_times.is_approximate(expression),
        "interval_seconds": schedule_times.typical_interval(expression, now),
        "next_runs": [_iso(moment) for moment in nexts],
        "last_fired_at": last.get("at") if last else None,
        "last_outcome": last.get("outcome") if last else None,
        "fires": fires,
        "workflows": reached,
        "health": health(item, now, reached),
    })
    return view


def api_list(table_ref=None, *, now=None, workflows=None):
    now = now or datetime.now(timezone.utc)
    if workflows is None:
        workflows = load_workflows()
    return 200, {
        "now": _iso(now),
        "schedules": [public_view(item, now=now, workflows=workflows)
                      for item in load_items(table_ref=table_ref)],
    }


def api_upcoming(hours=24, table_ref=None, *, now=None, workflows=None):
    """Every enabled schedule's fires in the next ``hours`` (max a week),
    soonest first. Schedules firing more often than hourly are summarized
    under ``frequent`` instead of flooding the list."""
    try:
        hours = int(hours)
    except (TypeError, ValueError):
        return 400, {"error": "hours must be a whole number"}
    if hours < 1 or hours > UPCOMING_MAX_HOURS:
        return 400, {"error": f"hours must be between 1 and {UPCOMING_MAX_HOURS}"}
    now = now or datetime.now(timezone.utc)
    until = now + timedelta(hours=hours)
    if workflows is None:
        workflows = load_workflows()
    upcoming, frequent = [], []
    for item in load_items(table_ref=table_ref):
        if not item.get("enabled", True):
            continue
        expression = item.get("expression") or ""
        name = item.get("schedule_id")
        reached = [workflow["id"] for workflow in listeners(name, workflows)
                   if workflow["enabled"]]
        summary = schedule_times.describe(expression)
        approximate = schedule_times.is_approximate(expression)
        interval = schedule_times.typical_interval(expression, now)
        if interval and interval <= FREQUENT_SECONDS:
            times = schedule_times.next_fires(expression, now, count=UPCOMING_MAX_HOURS * 60,
                                              until=until, anchor=_rate_anchor(item))
            if times:
                frequent.append({"schedule_id": name, "summary": summary,
                                 "count": len(times), "first_at": _iso(times[0]),
                                 "approximate": approximate, "workflows": reached})
            continue
        for moment in schedule_times.next_fires(expression, now, count=UPCOMING_LIMIT,
                                                until=until, anchor=_rate_anchor(item)):
            upcoming.append({"at": _iso(moment), "schedule_id": name, "summary": summary,
                             "approximate": approximate, "workflows": reached})
    upcoming.sort(key=lambda entry: (entry["at"], entry["schedule_id"]))
    return 200, {
        "now": _iso(now),
        "hours": hours,
        "upcoming": upcoming[:UPCOMING_LIMIT],
        "truncated": len(upcoming) > UPCOMING_LIMIT,
        "frequent": frequent,
    }


def _sqs():
    import boto3

    return boto3.client("sqs")


def api_run_now(name, operator, *, table_ref=None, queue=None, now=None):
    """Fire a schedule once, now, through the event queue — the same
    envelope a scheduled fire publishes, marked manual. A paused schedule
    runs too: Run now is how an operator tries it before resuming."""
    name = str(name or "").strip().lower()
    item = get_item(name, table_ref=table_ref)
    if not item:
        raise TriggerError(f"no schedule trigger named '{name}'")
    event = fire_event(name, manual=True, requested_by=operator, now=now)
    (queue if queue is not None else _sqs()).send_message(
        QueueUrl=os.environ["EVENT_QUEUE_URL"],
        MessageBody=json.dumps(event),
    )
    reached = [workflow["id"] for workflow in listeners(name, load_workflows())
               if workflow["enabled"]]
    return 202, {
        "accepted": True,
        "schedule_id": name,
        "event_id": event["id"],
        "workflows": reached,
        "runs": [{"workflow_id": workflow, "run_id": f"{workflow}:{event['id']}"}
                 for workflow in reached],
    }


def api_set_enabled(name, enabled, operator, *, table_ref=None, events_client=None,
                    target_arn=None):
    """Pause or resume: the stored item and its EventBridge rule state.
    The history notes who paused it, so silence after a pause is explained."""
    name = str(name or "").strip().lower()
    previous = get_item(name, table_ref=table_ref)
    if not previous:
        raise TriggerError(f"no schedule trigger named '{name}'")
    body = {"name": name, "expression": previous.get("expression"),
            "description": previous.get("description") or "", "enabled": bool(enabled)}
    item = build_item(body, operator, previous=previous)
    sync_rule(item, events_client=events_client, target_arn=target_arn)
    get_table(table_ref).put_item(Item=item)
    return 200, {"changed": bool(previous.get("enabled", True)) != bool(enabled),
                 **public_view(item)}


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
