import json
import logging
import os
import time
import uuid
from datetime import datetime, timezone

from . import execute


logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)


def normalize_payload(payload):
    if payload.get("Type") == "Notification" and isinstance(payload.get("Message"), str):
        payload = json.loads(payload["Message"])
    if payload.get("contract") == "inbound-email" and payload.get("version") == 1:
        return {
            "schema_version": "1.0",
            "id": payload["event_id"],
            "correlation_id": payload["event_id"],
            "connector": "email",
            "event": "message.received",
            "source": payload["route"],
            "occurred_at": payload["occurred_at"],
            "data": {
                "route": payload["route"],
                "message_id": payload["message_id"],
                "sender": payload["sender"],
                "recipients": payload["recipients"],
                "subject": payload["subject"],
                "date": payload.get("date") or payload["occurred_at"],
                "body": payload["body"],
                "html": payload["body"].get("html"),
                "attachments": payload["attachments"],
                "raw_mime": payload["raw_mime"],
            },
        }
    if payload.get("contract") == "inbound-email":
        raise ValueError(f"unsupported inbound-email contract version: {payload.get('version')}")
    if payload.get("schema") == "html-renderer.completed.v1":
        source = payload.get("context", {}).get("source_event", {})
        return {
            "schema_version": "1.0",
            "id": payload["job_id"],
            "correlation_id": source.get("correlation_id", payload["job_id"]),
            "connector": "renderer",
            "event": "job.completed",
            "source": "html-renderer",
            "occurred_at": payload["timestamp"],
            "data": {
                "job_id": payload["job_id"],
                "output": payload["output"],
                "content_type": payload.get("content_type"),
                "size_bytes": payload.get("size_bytes"),
                "checksum": payload.get("checksum"),
                "source_event": source,
                "message_id": source.get("data", {}).get("message_id"),
                "route": source.get("data", {}).get("route"),
            },
        }
    return payload


def _execution_id(workflow_id, action_id, event):
    return f"{workflow_id}:{action_id}:{event['id']}"


def _run_id(workflow_id, event):
    """One run = one workflow's handling of one trigger event."""
    return f"{workflow_id}:{event.get('id', '')}"


def _now_iso():
    return datetime.now(timezone.utc).isoformat()


def _trim(value, limit=6000):
    """Cap a captured step value so execution items stay far below the
    DynamoDB 400 KB limit; oversized values keep a JSON preview."""
    try:
        text = json.dumps(value, default=str)
    except (TypeError, ValueError):
        text = json.dumps(str(value))
    if len(text) <= limit:
        return value
    return {"truncated": True, "preview": text[:limit]}


def _is_pending(workflow_id, action_id, event, action_type=None):
    import boto3
    from botocore.exceptions import ClientError

    table = boto3.resource("dynamodb").Table(os.environ["EXECUTIONS_TABLE"])
    now = int(time.time())
    item = {
        "execution_id": _execution_id(workflow_id, action_id, event),
        "run_id": _run_id(workflow_id, event),
        "workflow_id": workflow_id,
        "action_id": action_id,
        "connector": event.get("connector"),
        "event_type": event.get("event"),
        "correlation_id": event.get("correlation_id") or event.get("id"),
        "status": "processing",
        "started_at": _now_iso(),
        "lease_until": now + 300,
        "expires_at": now + 90 * 86400,
        # Step telemetry for the run view: the action type and the event data
        # every action in the flow receives as its input.
        "input": _trim(event.get("data") or {}),
    }
    if action_type:
        item["action_type"] = action_type
    if event.get("occurred_at"):
        item["occurred_at"] = event["occurred_at"]
    try:
        table.put_item(
            Item=item,
            ConditionExpression="attribute_not_exists(execution_id) OR lease_until < :now",
            ExpressionAttributeValues={":now": now},
        )
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
            return False
        raise
    return True


def _mark_completed(workflow_id, action_id, event, output=None, duration_ms=None,
                    status="completed"):
    """Close out a step; ``status`` is ``completed`` or ``filtered`` (a filter
    stopped the chain — quiet, but visible in run history)."""
    import boto3

    sets = ["#status = :status", "finished_at = :finished", "expires_at = :expires"]
    names = {"#status": "status"}
    values = {
        ":status": status,
        ":finished": _now_iso(),
        ":expires": int(time.time()) + 90 * 86400,
    }
    if output is not None:
        sets.append("#output = :output")
        names["#output"] = "output"
        values[":output"] = _trim(output)
    if duration_ms is not None:
        sets.append("duration_ms = :duration")
        values[":duration"] = int(duration_ms)
    boto3.resource("dynamodb").Table(os.environ["EXECUTIONS_TABLE"]).update_item(
        Key={"execution_id": _execution_id(workflow_id, action_id, event)},
        UpdateExpression="SET " + ", ".join(sets),
        ExpressionAttributeNames=names,
        ExpressionAttributeValues=values,
    )


def _release_action(workflow_id, action_id, event, exc=None, duration_ms=None):
    import boto3

    now = int(time.time())
    message = (str(exc) or exc.__class__.__name__) if exc is not None else "Action failed"
    sets = [
        "#status = :failed", "finished_at = :finished", "#error = :error",
        "lease_until = :lease", "expires_at = :expires",
    ]
    names = {"#status": "status", "#error": "error"}
    values = {
        ":failed": "failed",
        ":finished": _now_iso(),
        ":error": message[:500],
        ":lease": now - 1,
        ":expires": now + 90 * 86400,
    }
    if duration_ms is not None:
        sets.append("duration_ms = :duration")
        values[":duration"] = int(duration_ms)
    boto3.resource("dynamodb").Table(os.environ["EXECUTIONS_TABLE"]).update_item(
        Key={"execution_id": _execution_id(workflow_id, action_id, event)},
        UpdateExpression="SET " + ", ".join(sets),
        ExpressionAttributeNames=names,
        ExpressionAttributeValues=values,
    )


def _schedule_event(payload):
    """Normalize a schedule trigger fire into a dapier event.

    The EventBridge target input replaces the whole event, so the payload
    names the trigger and the fire's id and time are minted here.
    """
    schedule_id = payload["schedule_id"]
    fired_at = datetime.now(timezone.utc).isoformat()
    return {
        "schema_version": "1.0",
        "id": f"{schedule_id}-{uuid.uuid4()}",
        "correlation_id": f"{schedule_id}-{fired_at}",
        "connector": "schedule",
        "event": "schedule.triggered",
        "source": schedule_id,
        "occurred_at": fired_at,
        "data": {
            "schedule": schedule_id,
            "utc_time": fired_at,
        },
    }


def handler(event, _context):
    if isinstance(event, dict) and event.get("trigger") == "schedule" and event.get("schedule_id"):
        # EventBridge invokes the function directly: the target input names
        # the trigger, and the envelope carries the fire's id and time.
        try:
            normalized = _schedule_event(event)
            execute(
                normalized,
                before_action=_is_pending,
                after_action=_mark_completed,
                on_action_error=_release_action,
            )
        except Exception:
            logger.exception("schedule trigger failed", extra={"schedule_id": event.get("schedule_id")})
            raise
        return {"executed": event["schedule_id"]}
    failures = []
    for record in event.get("Records", []):
        try:
            payload = json.loads(record["body"])
            payload = normalize_payload(payload)
            execute(
                payload,
                before_action=_is_pending,
                after_action=_mark_completed,
                on_action_error=_release_action,
            )
        except Exception:
            logger.exception("workflow record failed", extra={"message_id": record.get("messageId")})
            failures.append({"itemIdentifier": record["messageId"]})
    return {"batchItemFailures": failures}
