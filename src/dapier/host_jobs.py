"""Authenticated HTTP lease protocol for headless host jobs.

The Lambda owns SQS and DynamoDB. The host only presents a dedicated Dapier
API token and never receives AWS credentials. A task lease fences stale hosts;
the SQS receipt stays in DynamoDB rather than crossing the HTTP boundary.
"""

import json
import os
import time
import uuid
from email.utils import parseaddr

from .host_tasks import tasks_table
from .host_workers import checkin, meta_of

VISIBILITY_SECONDS = 120
WAIT_SECONDS = 10
MAX_LOG_CHARS = 12000

TERMINAL = frozenset({"succeeded", "failed", "timed_out", "interrupted"})


def queue_client(queue=None):
    if queue is not None:
        return queue
    import boto3

    return boto3.client("sqs")


def _queue_url(queue_url=None):
    return queue_url or os.environ["HOST_QUEUE_URL"]


def _row(table, task_id):
    return table.get_item(Key={"task_id": task_id}).get("Item") or {}


def _delete(queue, receipt, queue_url):
    queue.delete_message(QueueUrl=queue_url, ReceiptHandle=receipt)


def _set(table, task_id, fields, condition=None, condition_values=None):
    names, values, assignments = {}, {}, []
    for index, (key, value) in enumerate(fields.items()):
        names[f"#f{index}"] = key
        values[f":f{index}"] = value
        assignments.append(f"#f{index} = :f{index}")
    kwargs = {
        "Key": {"task_id": task_id},
        "UpdateExpression": "SET " + ", ".join(assignments),
        "ExpressionAttributeNames": names,
        "ExpressionAttributeValues": values,
    }
    if condition:
        kwargs["ConditionExpression"] = condition
        kwargs["ExpressionAttributeValues"].update(condition_values or {})
        if "#status" in condition:
            kwargs["ExpressionAttributeNames"]["#status"] = "status"
    table.update_item(**kwargs)


def claim(owner, body=None, *, table_ref=None, queue_ref=None, queue_url=None, now=None):
    """Lease one queued task or return None after a bounded long poll."""
    table, queue = tasks_table(table_ref), queue_client(queue_ref)
    # Presence first: an idle worker polls this every ~10s, so the check-in
    # doubles as the worker's heartbeat. Older workers send no meta and
    # simply never appear in the registry.
    meta = meta_of(body)
    if meta:
        checkin(owner, meta, table_ref=table, now=now)
    url = _queue_url(queue_url)
    response = queue.receive_message(
        QueueUrl=url, WaitTimeSeconds=WAIT_SECONDS, MaxNumberOfMessages=1,
        VisibilityTimeout=VISIBILITY_SECONDS,
    )
    messages = response.get("Messages") or []
    if not messages:
        return 200, {"job": None}
    receipt = messages[0]["ReceiptHandle"]
    try:
        message = json.loads(messages[0].get("Body") or "null")
    except (TypeError, ValueError):
        return 502, {"error": "Invalid host queue message"}
    if not isinstance(message, dict) or message.get("kind") != "agent" or not message.get("task_id"):
        return 502, {"error": "Invalid host queue message"}
    task_id = message["task_id"]
    row = _row(table, task_id)
    status = row.get("status")
    if status in TERMINAL:
        _notify(table, task_id, row)
        _delete(queue, receipt, url)
        return 200, {"job": None}
    if status == "running":
        if int(row.get("lease_until") or 0) <= int(now or time.time()):
            # Use an explicit conditional expression so an old receipt cannot
            # overwrite a newer terminal result.
            table.update_item(
                Key={"task_id": task_id},
                UpdateExpression="SET #status = :interrupted, finished_at = :now, #error = :reason",
                ConditionExpression="#status = :running AND lease_id = :lease AND lease_until <= :now",
                ExpressionAttributeNames={"#status": "status", "#error": "error"},
                ExpressionAttributeValues={
                    ":interrupted": "interrupted", ":running": "running",
                    ":lease": row.get("lease_id"), ":now": int(now or time.time()),
                    ":reason": "The worker lost its lease before reporting completion",
                },
            )
            _notify(table, task_id, _row(table, task_id))
        _delete(queue, receipt, url)
        return 200, {"job": None}
    if status != "queued":
        return 502, {"error": "Host task row is missing or invalid"}
    now = int(now or time.time())
    lease_id = uuid.uuid4().hex
    try:
        table.update_item(
            Key={"task_id": task_id},
            UpdateExpression=(
                "SET #status = :running, lease_id = :lease, lease_owner = :owner, "
                "lease_until = :until, receipt_handle = :receipt, started_at = :now"
            ),
            ConditionExpression="#status = :queued",
            ExpressionAttributeNames={"#status": "status"},
            ExpressionAttributeValues={
                ":running": "running", ":queued": "queued", ":lease": lease_id,
                ":owner": owner, ":until": now + VISIBILITY_SECONDS,
                ":receipt": receipt, ":now": now,
            },
        )
    except Exception as exc:
        if getattr(exc, "response", {}).get("Error", {}).get("Code") != "ConditionalCheckFailedException":
            raise
        _delete(queue, receipt, url)
        return 200, {"job": None}
    if meta:
        checkin(owner, meta, task_id=task_id, table_ref=table, now=now)
    return 200, {"job": {"task_id": task_id, "lease_id": lease_id,
                          "engine": message.get("engine") or "claude",
                          "workspace": message.get("workspace") or "",
                          "prompt": message.get("prompt") or ""}}


def _owned(table, task_id, lease_id, owner):
    row = _row(table, task_id)
    if not row or row.get("lease_id") != lease_id or row.get("lease_owner") != owner:
        return None
    return row


def heartbeat(body, owner, *, table_ref=None, queue_ref=None, queue_url=None, now=None):
    if not isinstance(body, dict) or not body.get("task_id") or not body.get("lease_id"):
        return 400, {"error": "Task ID and lease ID are required"}
    table = tasks_table(table_ref)
    # Refresh presence even when the lease has lapsed: the worker is alive
    # either way, and the next claim will reconcile the task state.
    meta = meta_of(body)
    if meta:
        checkin(owner, meta, task_id=body["task_id"], table_ref=table, now=now)
    row = _owned(table, body["task_id"], body["lease_id"], owner)
    if not row or row.get("status") != "running":
        return 409, {"error": "Task lease is no longer active"}
    now = int(now or time.time())
    if int(row.get("lease_until") or 0) < now:
        return 409, {"error": "Task lease expired"}
    queue_client(queue_ref).change_message_visibility(
        QueueUrl=_queue_url(queue_url), ReceiptHandle=row["receipt_handle"],
        VisibilityTimeout=VISIBILITY_SECONDS,
    )
    _set(table, body["task_id"], {"lease_until": now + VISIBILITY_SECONDS},
         "lease_id = :lease AND lease_owner = :owner AND #status = :running",
         {":lease": body["lease_id"], ":owner": owner, ":running": "running"})
    return 200, {"lease_until": now + VISIBILITY_SECONDS}


def finish(body, owner, *, table_ref=None, queue_ref=None, queue_url=None, now=None,
           ses_ref=None):
    if not isinstance(body, dict) or not body.get("task_id") or not body.get("lease_id"):
        return 400, {"error": "Task ID and lease ID are required"}
    status = body.get("status")
    if status not in ("succeeded", "failed", "timed_out"):
        return 400, {"error": "Invalid terminal status"}
    meta = meta_of(body)
    table = tasks_table(table_ref)
    row = _owned(table, body["task_id"], body["lease_id"], owner)
    if not row or row.get("status") not in ({"running"} | TERMINAL):
        return 409, {"error": "Task lease is no longer active"}
    if row["status"] == "running":
        summary = str(body.get("summary") or "")[:2000]
        code = body.get("exit_code")
        if code is not None and (not isinstance(code, int) or isinstance(code, bool)):
            return 400, {"error": "Invalid exit code"}
        logs = body.get("logs")
        if logs is not None:
            if (not isinstance(logs, dict) or
                    any(not isinstance(logs.get(key, ""), str) for key in ("stdout", "stderr"))):
                return 400, {"error": "Invalid agent logs"}
            logs = {"stdout": logs.get("stdout", "")[-MAX_LOG_CHARS:],
                    "stderr": logs.get("stderr", "")[-MAX_LOG_CHARS:],
                    "truncated": bool(logs.get("truncated")) or any(
                        len(logs.get(key, "")) > MAX_LOG_CHARS for key in ("stdout", "stderr"))}
        fields = {"status": status, "finished_at": int(now or time.time()),
                  "summary": summary, "error": "" if status == "succeeded" else summary[:500]}
        if logs is not None:
            fields["logs"] = logs
        if code is not None:
            fields["exit_code"] = code
        _set(table, body["task_id"], fields,
             "lease_id = :lease AND lease_owner = :owner AND #status = :running",
             {":lease": body["lease_id"], ":owner": owner, ":running": "running"})
        row = _row(table, body["task_id"])
    _notify(table, body["task_id"], row, ses_ref=ses_ref)
    _delete(queue_client(queue_ref), row["receipt_handle"], _queue_url(queue_url))
    if meta:
        checkin(owner, meta, finished=(body["task_id"], row["status"]),
                table_ref=table, now=now)
    return 200, {"task_id": body["task_id"], "status": row["status"]}


def _notify(table, task_id, row, ses_ref=None):
    address = str(row.get("notify_to") or "").strip()
    if not address or row.get("notified_at"):
        return
    if parseaddr(address)[1] != address or "@" not in address:
        return
    if ses_ref is None:
        import boto3

        ses_ref = boto3.client(
            "ses", region_name=os.environ.get("DAPIER_EMAIL_REGION") or None)
    summary = str(row.get("summary") or row.get("error") or "")[:1500]
    title = str(row.get("email_subject") or "Agent task")[:150]
    ses_ref.send_email(
        Source=str(row.get("notify_from") or os.environ["DAPIER_EMAIL_SENDER"]),
        Destination={"ToAddresses": [address]},
        Message={"Subject": {"Data": f"Agent {row['status']}: {title}"[:250], "Charset": "utf-8"},
                 "Body": {"Text": {"Data": f"Task: {task_id}\nStatus: {row['status']}\n\n{summary}",
                                   "Charset": "utf-8"}}},
    )
    _set(table, task_id, {"notified_at": int(time.time())})
