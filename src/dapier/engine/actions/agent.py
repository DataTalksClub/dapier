"""agent action: enqueue a host job. The Lambda does not start a session."""
import json
import os
import time

from .templating import render


def build_message(action, event, workflow_id, steps=None):
    """The host-queue body. Pure aside from template rendering."""
    prompt_template = action.get("prompt")
    if not str(prompt_template or "").strip():
        raise ValueError("agent prompt is required")
    workspace = str(action.get("workspace") or "").strip()
    if not workspace:
        raise ValueError("agent workspace is required")
    engine = str(action.get("engine") or "").strip()
    tag_prefix = str(action.get("tag_prefix") or "agent").strip() or "agent"
    action_id = str(action.get("id") or "agent")
    task_id = f"agent:{workflow_id}:{event.get('id', '')}:{action_id}"
    return {
        "kind": "agent",
        "task_id": task_id,
        "engine": engine,
        "workspace": workspace,
        "tag_prefix": tag_prefix,
        "prompt": render(str(prompt_template), event, steps),
    }


def run_agent(action, event, workflow_id, steps=None, *, queue=None, tasks=None, queue_url=None):
    """Write the task row, enqueue it, and return without waiting."""
    message = build_message(action, event, workflow_id, steps)
    now = int(time.time())
    item = {
        **message,
        "status": "starting",
        "created_at": now,
        "expires_at": now + 90 * 86400,
    }
    table = tasks
    if table is None:
        import boto3

        table = boto3.resource("dynamodb").Table(os.environ["HOST_TASKS_TABLE"])
    table.put_item(Item=item)
    sender = queue
    if sender is None:
        import boto3

        sender = boto3.client("sqs")
    sender.send_message(
        QueueUrl=queue_url or os.environ["HOST_QUEUE_URL"],
        MessageBody=json.dumps(message),
    )
    return {"task_id": message["task_id"], "status": "queued"}
