import json
import logging
import os
import time

from .engine import execute


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


def _is_pending(workflow_id, action_id, event):
    import boto3
    from botocore.exceptions import ClientError

    table = boto3.resource("dynamodb").Table(os.environ["EXECUTIONS_TABLE"])
    now = int(time.time())
    try:
        table.put_item(
            Item={
                "execution_id": _execution_id(workflow_id, action_id, event),
                "status": "processing",
                "lease_until": now + 300,
                "expires_at": now + 90 * 86400,
            },
            ConditionExpression="attribute_not_exists(execution_id) OR lease_until < :now",
            ExpressionAttributeValues={":now": now},
        )
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
            return False
        raise
    return True


def _mark_completed(workflow_id, action_id, event):
    import boto3

    boto3.resource("dynamodb").Table(os.environ["EXECUTIONS_TABLE"]).put_item(Item={
        "execution_id": _execution_id(workflow_id, action_id, event),
        "status": "completed",
        "expires_at": int(time.time()) + 90 * 86400,
    })


def _release_action(workflow_id, action_id, event):
    import boto3

    boto3.resource("dynamodb").Table(os.environ["EXECUTIONS_TABLE"]).delete_item(
        Key={"execution_id": _execution_id(workflow_id, action_id, event)},
        ConditionExpression="#status = :processing",
        ExpressionAttributeNames={"#status": "status"},
        ExpressionAttributeValues={":processing": "processing"},
    )


def handler(event, _context):
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
