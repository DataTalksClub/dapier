"""Agent action: enqueue a headless host job."""
import json
import os
import time
from ...triggers.email_from import bare_address, sender_addresses
from .base import _safe_filename

from .templating import render

# The trigger event's stored attachments ride the host job as s3 pointers so
# the worker can stage the files into the agent's workspace. Pointers are a
# few hundred bytes each; the cap keeps a pathological event from bloating
# the task row and queue message.
MAX_ATTACHMENTS = 10


def collect_attachments(action, event):
    """The stored attachments to hand the worker, as pointer descriptors.

    On by default for events that carry stored attachments (an inbound email
    with files); ``attachments: off`` on the action opts out. Entries without
    a stored s3 body (inline-only attachment stubs) are skipped.
    """
    if str(action.get("attachments", "")).strip().lower() in ("off", "false", "no"):
        return []
    descriptors = []
    for entry in (event.get("data") or {}).get("attachments") or []:
        s3 = entry.get("s3") if isinstance(entry, dict) else None
        if not (isinstance(s3, dict) and s3.get("bucket") and s3.get("key")):
            continue
        descriptors.append({
            "filename": _safe_filename(entry.get("filename")),
            "size": entry.get("size"),
            "content_type": entry.get("content_type"),
            "s3": {"bucket": s3["bucket"], "key": s3["key"]},
        })
        if len(descriptors) >= MAX_ATTACHMENTS:
            break
    return descriptors


def build_message(action, event, workflow_id, steps=None):
    """The host-queue body. Pure aside from template rendering."""
    prompt_template = action.get("prompt")
    if not str(prompt_template or "").strip():
        raise ValueError("agent prompt is required")
    # An omitted workspace starts the run in the host worker's configured
    # root. The Lambda must never guess a path on another machine.
    workspace = str(action.get("workspace") or "").strip()
    engine = str(action.get("engine") or "").strip()
    tag_prefix = str(action.get("tag_prefix") or "agent").strip() or "agent"
    action_id = str(action.get("id") or "agent")
    task_id = f"agent:{workflow_id}:{event.get('id', '')}:{action_id}"
    notify_to = (render(str(action["notify_to"]), event, steps).strip()
                 if "notify_to" in action else
                 (sender_addresses(event)[:1] or [""])[0]
                 if event.get("connector") == "email" else "")
    if notify_to and bare_address(notify_to) != notify_to.lower():
        raise ValueError("agent notify_to must be one email address")
    notify_from = str(action.get("notify_from") or "").strip().lower()
    if notify_from and bare_address(notify_from) != notify_from:
        raise ValueError("agent notify_from must be one email address")
    return {
        "kind": "agent",
        "task_id": task_id,
        "engine": engine,
        "workspace": workspace,
        "tag_prefix": tag_prefix,
        "prompt": render(str(prompt_template), event, steps),
        "notify_to": notify_to,
        "notify_from": notify_from,
        "email_subject": str((event.get("data") or {}).get("subject") or "")[:200],
        "attachments": collect_attachments(action, event),
    }


def run_agent(action, event, workflow_id, steps=None, *, queue=None, tasks=None, queue_url=None):
    """Write the task row, enqueue it, and return without waiting."""
    message = build_message(action, event, workflow_id, steps)
    now = int(time.time())
    item = {
        **message,
        "status": "queued",
        "created_at": now,
        "expires_at": now + 90 * 86400,
    }
    table = tasks
    if table is None:
        import boto3

        table = boto3.resource("dynamodb").Table(os.environ["HOST_TASKS_TABLE"])
    from botocore.exceptions import ClientError

    try:
        table.put_item(Item=item, ConditionExpression="attribute_not_exists(task_id)")
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") != "ConditionalCheckFailedException":
            raise
        existing = table.get_item(Key={"task_id": message["task_id"]}).get("Item") or {}
        if existing.get("status") not in ("queued", "claimed"):
            return {"task_id": message["task_id"], "status": existing.get("status", "unknown")}
    sender = queue
    if sender is None:
        import boto3

        sender = boto3.client("sqs")
    sender.send_message(
        QueueUrl=queue_url or os.environ["HOST_QUEUE_URL"],
        MessageBody=json.dumps(message),
    )
    return {"task_id": message["task_id"], "status": "queued"}
