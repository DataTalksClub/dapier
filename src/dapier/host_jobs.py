"""Authenticated HTTP lease protocol for headless host jobs.

The Lambda owns SQS and DynamoDB. The host only presents a dedicated Dapier
API token and never receives AWS credentials. A task lease fences stale hosts;
the SQS receipt stays in DynamoDB rather than crossing the HTTP boundary.
"""

import base64
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
# One attachment download returns at most this many raw bytes (base64 in the
# JSON envelope) so the response stays under API Gateway's 10 MB payload cap.
MAX_ATTACHMENT_CHUNK = 4_000_000

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
    try:
        meta = meta_of(body)
    except ValueError as exc:
        return 400, {"error": str(exc)}
    if meta:
        checkin(owner, meta, table_ref=table, now=now)
    routed = _claim_routed(table, owner, meta, int(now or time.time()))
    if routed:
        return 200, {"job": routed}
    # Codex workers must never consume the legacy Claude queue.
    if meta and meta.get("engine") == "codex":
        return 200, {"job": None}
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
    # The persisted requirements are authoritative, including when an old
    # enqueue retry or stale message puts a routed task in the legacy queue.
    if row.get("requires") or row.get("engine") == "codex":
        _delete(queue, receipt, url)
        return 200, {"job": None}
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
                          "prompt": message.get("prompt") or "",
                          "attachments": message.get("attachments") or []}}


def _claim_routed(table, owner, meta, now):
    """Conditionally lease matching tasks from the table, oldest first.

    Restricted jobs bypass SQS so workers without capabilities never receive
    them. Expired leases are interrupted, never automatically executed twice.
    """
    from botocore.exceptions import ClientError

    items, start = [], None
    while True:
        page = table.scan(**({"ExclusiveStartKey": start} if start else {}))
        items.extend(row for row in page.get("Items", [])
                     if row.get("kind") == "agent" and
                     (row.get("requires") or row.get("engine") == "codex"))
        start = page.get("LastEvaluatedKey")
        if not start:
            break
    items.sort(key=lambda row: (int(row.get("created_at") or 0), row["task_id"]))
    for row in items:
        if row.get("status") == "running" and int(row.get("lease_until") or 0) <= now:
            try:
                _set(table, row["task_id"], {
                    "status": "interrupted", "finished_at": now,
                    "error": "The worker lost its lease before reporting completion",
                }, "#status = :running AND lease_id = :lease AND lease_until <= :now",
                     {":running": "running", ":lease": row.get("lease_id"), ":now": now})
            except ClientError as exc:
                if exc.response["Error"]["Code"] != "ConditionalCheckFailedException":
                    raise
            row = _row(table, row["task_id"])
        if row.get("status") in TERMINAL:
            _notify(table, row["task_id"], row)
            continue
        if (not meta or row.get("status") != "queued" or
                (row.get("engine") or "claude") != (meta.get("engine") or "claude") or
                not set(row.get("requires") or []).issubset(meta.get("capabilities") or [])):
            continue
        lease = uuid.uuid4().hex
        try:
            _set(table, row["task_id"], {
                "status": "running", "lease_id": lease, "lease_owner": owner,
                "lease_until": now + VISIBILITY_SECONDS, "started_at": now,
            }, "#status = :queued", {":queued": "queued"})
        except ClientError as exc:
            if exc.response["Error"]["Code"] != "ConditionalCheckFailedException":
                raise
            continue
        checkin(owner, meta, task_id=row["task_id"], table_ref=table, now=now)
        return {"task_id": row["task_id"], "lease_id": lease,
                "engine": row.get("engine") or "claude",
                "workspace": row.get("workspace") or "", "prompt": row.get("prompt") or "",
                "attachments": row.get("attachments") or [], "requires": row.get("requires") or []}


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
    try:
        meta = meta_of(body)
    except ValueError as exc:
        return 400, {"error": str(exc)}
    if meta:
        checkin(owner, meta, task_id=body["task_id"], table_ref=table, now=now)
    row = _owned(table, body["task_id"], body["lease_id"], owner)
    if not row or row.get("status") != "running":
        return 409, {"error": "Task lease is no longer active"}
    now = int(now or time.time())
    if int(row.get("lease_until") or 0) < now:
        return 409, {"error": "Task lease expired"}
    if row.get("receipt_handle"):
        queue_client(queue_ref).change_message_visibility(
            QueueUrl=_queue_url(queue_url), ReceiptHandle=row["receipt_handle"],
            VisibilityTimeout=VISIBILITY_SECONDS,
        )
    _set(table, body["task_id"], {"lease_until": now + VISIBILITY_SECONDS},
         "lease_id = :lease AND lease_owner = :owner AND #status = :running",
         {":lease": body["lease_id"], ":owner": owner, ":running": "running"})
    return 200, {"lease_until": now + VISIBILITY_SECONDS}


def attachment(body, owner, *, table_ref=None, now=None, s3_ref=None):
    """One chunk of a claimed job's stored attachment.

    Serves only the lease owner while the lease is live, so a stale or
    foreign worker can never pull a trigger event's files. The bytes stay in
    S3; this reads them straight from the recorded s3 pointer in chunks that
    fit the HTTPS response cap.
    """
    if not isinstance(body, dict) or not body.get("task_id") or not body.get("lease_id"):
        return 400, {"error": "Task ID and lease ID are required"}
    table = tasks_table(table_ref)
    row = _owned(table, body["task_id"], body["lease_id"], owner)
    if not row or row.get("status") != "running":
        return 409, {"error": "Task lease is no longer active"}
    if int(row.get("lease_until") or 0) < int(now or time.time()):
        return 409, {"error": "Task lease expired"}
    try:
        index = int(body.get("index"))
    except (TypeError, ValueError):
        return 400, {"error": "Invalid attachment index"}
    attachments = row.get("attachments") or []
    if not 0 <= index < len(attachments):
        return 404, {"error": "No such attachment"}
    ref = (attachments[index].get("s3") or {}) if isinstance(attachments[index], dict) else {}
    if not (ref.get("bucket") and ref.get("key")):
        return 404, {"error": "Attachment has no stored file"}
    try:
        offset = max(0, int(body.get("offset") or 0))
    except (TypeError, ValueError):
        return 400, {"error": "Invalid chunk offset"}
    length = MAX_ATTACHMENT_CHUNK
    if body.get("length") is not None:
        try:
            length = int(body["length"])
        except (TypeError, ValueError):
            return 400, {"error": "Invalid chunk length"}
        if not 0 < length <= MAX_ATTACHMENT_CHUNK:
            return 400, {"error": "Invalid chunk length"}
    if s3_ref is None:
        import boto3

        s3_ref = boto3.client("s3")
    from botocore.exceptions import ClientError

    try:
        obj = s3_ref.get_object(
            Bucket=ref["bucket"], Key=ref["key"],
            Range=f"bytes={offset}-{offset + length - 1}")
        chunk = obj["Body"].read()
    except ClientError as exc:
        code = str(exc.response.get("Error", {}).get("Code") or "")
        if "NoSuchKey" in code or code == "404":
            return 404, {"error": "Attachment file is gone"}
        if "InvalidRange" in code:
            return 400, {"error": "Chunk offset is past the end of the file"}
        raise
    size = attachments[index].get("size")
    content_range = obj.get("ContentRange") or ""
    if content_range.rsplit("/", 1)[-1].isdigit():
        size = int(content_range.rsplit("/", 1)[-1])
    done = len(chunk) < length or (size is not None and offset + len(chunk) >= size)
    return 200, {
        "filename": attachments[index].get("filename") or "attachment",
        "content_type": obj.get("ContentType") or attachments[index].get("content_type"),
        "size": size,
        "offset": offset,
        "b64": base64.b64encode(chunk).decode(),
        "done": done,
    }


def finish(body, owner, *, table_ref=None, queue_ref=None, queue_url=None, now=None,
           ses_ref=None):
    if not isinstance(body, dict) or not body.get("task_id") or not body.get("lease_id"):
        return 400, {"error": "Task ID and lease ID are required"}
    status = body.get("status")
    if status not in ("succeeded", "failed", "timed_out"):
        return 400, {"error": "Invalid terminal status"}
    try:
        meta = meta_of(body)
    except ValueError as exc:
        return 400, {"error": str(exc)}
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
    if row.get("receipt_handle"):
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
