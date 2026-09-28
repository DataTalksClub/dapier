"""Host worker: long-poll HostQueue and dispatch on ``kind``.

``agent`` starts one Aplexer session. The session client is injectable.
Aplexer is imported only when ``serve`` builds the real client.
"""

import json
import os
import time
import uuid
from pathlib import Path


WAIT_SECONDS = 20
VISIBILITY_SECONDS = 120
READY_TIMEOUT = 30
READY_PHASES = frozenset({"running", "waiting", "idle", "ready"})


class TaskBook:
    def __init__(self, table):
        self.table = table

    def begin(self, message):
        from botocore.exceptions import ClientError

        item = {
            "task_id": message["task_id"],
            "kind": message.get("kind"),
            "status": "starting",
            "prompt": message.get("prompt") or "",
            "engine": message.get("engine") or "",
            "workspace": message.get("workspace") or "",
            "tag_prefix": message.get("tag_prefix") or "agent",
            "created_at": int(time.time()),
        }
        try:
            self.table.put_item(
                Item=item,
                ConditionExpression="attribute_not_exists(task_id)",
            )
        except ClientError as exc:
            code = exc.response.get("Error", {}).get("Code")
            if code != "ConditionalCheckFailedException":
                raise
            return self.table.get_item(Key={"task_id": message["task_id"]}).get("Item") or item
        return item

    def save(self, task_id, **fields):
        names, values, sets = {}, {}, []
        for index, (key, value) in enumerate(fields.items()):
            names[f"#k{index}"] = key
            values[f":v{index}"] = value
            sets.append(f"#k{index} = :v{index}")
        if not sets:
            return
        self.table.update_item(
            Key={"task_id": task_id},
            UpdateExpression="SET " + ", ".join(sets),
            ExpressionAttributeNames=names,
            ExpressionAttributeValues=values,
        )


def _phase(status):
    if isinstance(status, dict):
        return status.get("phase")
    return getattr(status, "phase", None)


def wait_ready(sessions, selector, *, timeout=READY_TIMEOUT, sleep=time.sleep):
    deadline = time.monotonic() + timeout
    while True:
        if _phase(sessions.status(selector)) in READY_PHASES:
            return True
        if time.monotonic() >= deadline:
            return False
        sleep(0.2)


def handle_agent(message, *, tasks, sessions, sleep=time.sleep, ready_timeout=READY_TIMEOUT):
    """Return ``ack`` or ``retry``. A task id starts at most one session."""
    row = tasks.begin(message)
    status = row.get("status")
    if status in ("started", "ignored"):
        return "ack"
    task_id = message["task_id"]
    prompt = (row.get("prompt") or message.get("prompt") or "")
    if not prompt.endswith("\n"):
        prompt += "\n"

    def fail(error):
        tasks.save(task_id, error=str(error)[:500], status="starting")
        return "retry"

    try:
        if row.get("session_id") and not row.get("sent_at"):
            session_id = row["session_id"]
            if not wait_ready(sessions, session_id, timeout=ready_timeout, sleep=sleep):
                return fail("session was not ready for input")
            sessions.send(session_id, prompt.encode())
            tasks.save(task_id, status="started", sent_at=int(time.time()), error="")
            return "ack"
        if row.get("session_id") and row.get("sent_at"):
            tasks.save(task_id, status="started")
            return "ack"
        workspace = str(message.get("workspace") or row.get("workspace") or "")
        if not workspace or not Path(workspace).is_dir():
            return fail(f"workspace is not a directory: {workspace}")
        prefix = str(message.get("tag_prefix") or row.get("tag_prefix") or "agent")
        tag = f"{prefix}-{uuid.uuid4().hex[:8]}"
        engine = str(message.get("engine") or row.get("engine") or "").strip() or None
        session = sessions.start(workspace=workspace, tag=tag, engine=engine, fresh=True)
        session_id = getattr(session, "id", None) or session["id"]
        tasks.save(task_id, session_id=session_id, tag=getattr(session, "tag", None) or tag, status="starting")
        if not wait_ready(sessions, session_id, timeout=ready_timeout, sleep=sleep):
            return fail("session was not ready for input")
        sessions.send(session_id, prompt.encode())
        tasks.save(task_id, status="started", sent_at=int(time.time()), error="")
        return "ack"
    except Exception as exc:
        return fail(exc)


def dispatch(message, **deps):
    kind = (message or {}).get("kind")
    if kind == "agent":
        return handle_agent(message, **deps)
    return "retry"


def receive_once(queue, queue_url):
    return queue.receive_message(
        QueueUrl=queue_url,
        WaitTimeSeconds=WAIT_SECONDS,
        MaxNumberOfMessages=1,
        VisibilityTimeout=VISIBILITY_SECONDS,
    )


def poll_once(queue, queue_url, **deps):
    response = receive_once(queue, queue_url)
    for message in response.get("Messages") or []:
        try:
            body = json.loads(message.get("Body") or "null")
        except ValueError:
            body = None
        decision = dispatch(body, **deps) if isinstance(body, dict) else "ack"
        if decision == "ack":
            queue.delete_message(QueueUrl=queue_url, ReceiptHandle=message["ReceiptHandle"])
    return response


def serve(max_loops=None, queue=None, sessions=None):
    queue_url = os.environ["HOST_QUEUE_URL"]
    if queue is None:
        import boto3

        queue = boto3.client("sqs")
    if sessions is None:
        from aplexer import Client

        client = Client(state_dir=os.environ.get("APLEXER_STATE_DIR") or None)
        sessions = _AplexerSessions(client)
    import boto3

    tasks = TaskBook(boto3.resource("dynamodb").Table(os.environ["HOST_TASKS_TABLE"]))
    loops = 0
    while max_loops is None or loops < max_loops:
        poll_once(queue, queue_url, tasks=tasks, sessions=sessions)
        loops += 1


class _AplexerSessions:
    def __init__(self, client):
        self.client = client

    def start(self, *, workspace, tag, engine, fresh):
        return self.client.start(
            workspace=workspace, tag=tag, engine=engine, profile=None, fresh=fresh,
        )

    def status(self, selector):
        session = self.client.status(selector)
        return {"phase": getattr(session, "phase", None)}

    def send(self, selector, data):
        return self.client.send(selector, data)
