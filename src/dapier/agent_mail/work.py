"""Turn one queued agent-mail message into a single Aplexer session.

``handle_delivery`` is the whole decision: ignore and acknowledge, start
once and send, or leave the message for retry. ``serve`` is what
``dapier agent-mail work`` runs. It is the only place that constructs an
Aplexer client, and only after the process is actually polling.
"""

import json
import os
import time
import uuid
from email import policy
from email.parser import BytesParser
from pathlib import Path

from ..triggers.agent_mailboxes import MAILBOX_NAME, bare_address


READY_PHASES = frozenset({"running", "waiting", "idle", "ready"})
WAIT_SECONDS = 20
VISIBILITY_SECONDS = 120
READY_TIMEOUT = 30


def sender_allowed(from_header, addresses):
    bare = bare_address(from_header)
    allowed = {str(address).strip().lower() for address in addresses or []}
    return bool(bare) and bare in allowed


def rule_matches(rule, subject, body):
    from ..engine.matching import _matches_filter

    field = rule.get("field")
    value = subject if field == "subject" else body if field == "body" else None
    if value is None:
        return False
    try:
        return _matches_filter(value, {rule.get("operator"): rule.get("value")})
    except KeyError:
        return False


def rules_allow(rules, subject, body):
    """Every rule must match. A body rule sees ``body``; subject rules do not need it."""
    for rule in rules or []:
        if rule.get("field") == "body" and body is None:
            continue
        if not rule_matches(rule, subject, body if body is not None else ""):
            return False
    return True


def body_rules_pending(rules):
    return any(rule.get("field") == "body" for rule in rules or [])


def build_prompt(*, instructions, sender, subject, attachment_paths, plain, raw_location):
    """The text handed to the session, always ending in a newline."""
    blocks = []
    instructions = str(instructions or "").strip()
    if instructions:
        blocks.append(instructions)
    header = [
        f"From: {sender or ''}",
        f"Subject: {subject or ''}",
        "Attachments: " + (", ".join(attachment_paths) if attachment_paths else "none"),
    ]
    blocks.append("\n".join(header))
    if plain and str(plain).strip():
        blocks.append(str(plain).strip())
    else:
        blocks.append(
            "This message has no plain-text body. "
            f"The raw message is {raw_location}."
        )
    return "\n\n".join(blocks) + "\n"


def _safe_filename(name):
    base = Path(str(name or "")).name
    if not base or base in (".", ".."):
        return ""
    return base.replace("\x00", "")


def parse_message(raw, dest_dir):
    """Plain body and attachment paths written under ``dest_dir``."""
    message = BytesParser(policy=policy.default).parsebytes(raw)
    part = message.get_body(preferencelist=("plain",))
    plain = None
    if part is not None:
        try:
            content = part.get_content()
        except (LookupError, UnicodeDecodeError):
            content = None
        if isinstance(content, str):
            plain = content
    paths = []
    dest = Path(dest_dir)
    dest.mkdir(parents=True, exist_ok=True)
    for piece in message.walk():
        if piece.get_content_maintype() == "multipart":
            continue
        filename = _safe_filename(piece.get_filename())
        if not filename:
            continue
        payload = piece.get_payload(decode=True)
        if payload is None:
            continue
        target = dest / filename
        target.write_bytes(payload)
        paths.append(str(target))
    return plain, paths


def _phase(status):
    if isinstance(status, dict):
        return status.get("phase")
    return getattr(status, "phase", None)


def wait_ready(sessions, selector, *, timeout=READY_TIMEOUT, sleep=time.sleep):
    deadline = time.monotonic() + timeout
    while True:
        status = sessions.status(selector)
        if _phase(status) in READY_PHASES:
            return True
        if time.monotonic() >= deadline:
            return False
        sleep(0.2)


class TaskBook:
    """Idempotency rows. ``begin`` loses the race to a row that already exists."""

    def __init__(self, table):
        self.table = table

    def begin(self, task_id, fields):
        from botocore.exceptions import ClientError

        item = {"task_id": task_id, "status": "starting", **fields}
        try:
            self.table.put_item(
                Item=item,
                ConditionExpression="attribute_not_exists(task_id)",
            )
        except ClientError as exc:
            code = exc.response.get("Error", {}).get("Code")
            if code != "ConditionalCheckFailedException":
                raise
            return self.table.get_item(Key={"task_id": task_id}).get("Item") or item
        return item

    def save(self, task_id, **fields):
        names = {}
        values = {}
        sets = []
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


def _now_iso():
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()


def _task_id(message_id):
    text = str(message_id or "").strip()
    return f"email:{text}" if text else f"email:{uuid.uuid4().hex}"


def handle_delivery(body, *, load_mailbox, load_senders, open_object, tasks, sessions,
                    sleep=time.sleep, ready_timeout=READY_TIMEOUT):
    """Accept or ignore one queue body.

    Returns ``ack`` when the message must leave the queue, ``retry`` when it
    must stay. A session is started at most once per message id.
    """
    if not isinstance(body, dict):
        return "ack"
    message_id = body.get("message_id") or ""
    task_id = _task_id(message_id)
    mailbox_name = str(body.get("mailbox") or MAILBOX_NAME)
    row = tasks.begin(task_id, {
        "mailbox": mailbox_name,
        "from": body.get("from") or "",
        "subject": body.get("subject") or "",
        "s3": body.get("s3") or {},
        "created_at": _now_iso(),
        "expires_at": int(time.time()) + 90 * 86400,
    })
    status = row.get("status")
    if status in ("started", "ignored"):
        return "ack"
    if row.get("session_id") and row.get("sent_at"):
        tasks.save(task_id, status="started")
        return "ack"

    def fail(error):
        tasks.save(task_id, error=str(error)[:500], status="starting")
        return "retry"

    try:
        mailbox = load_mailbox(mailbox_name) or {}
        if row.get("session_id") and not row.get("sent_at"):
            return _send_existing(
                body, mailbox, row, tasks, sessions, open_object, task_id,
                sleep=sleep, ready_timeout=ready_timeout,
            )
        if not mailbox or not mailbox.get("enabled", True):
            tasks.save(task_id, status="ignored", error="mailbox disabled")
            return "ack"
        senders = load_senders()
        if not sender_allowed(body.get("from"), senders):
            tasks.save(task_id, status="ignored")
            return "ack"
        rules = mailbox.get("rules") or []
        if not rules_allow(
            [rule for rule in rules if rule.get("field") != "body"],
            body.get("subject") or "",
            "",
        ):
            tasks.save(task_id, status="ignored")
            return "ack"
        workspace = str(mailbox.get("workspace") or "")
        if not workspace or not Path(workspace).is_dir():
            return fail(f"workspace is not a directory: {workspace}")
        raw, plain, paths, location = _materialize(body, mailbox, task_id, open_object)
        if raw is None:
            return fail("the stored message could not be read")
        if not rules_allow(rules, body.get("subject") or "", plain or ""):
            tasks.save(task_id, status="ignored")
            return "ack"
        prefix = str(mailbox.get("tag_prefix") or "mail")
        tag = f"{prefix}-{uuid.uuid4().hex[:8]}"
        engine = str(mailbox.get("engine") or "").strip() or None
        profile = str(mailbox.get("profile") or "").strip() or None
        session = sessions.start(
            workspace=workspace, tag=tag, engine=engine, profile=profile, fresh=True,
        )
        session_id = getattr(session, "id", None) or session["id"]
        used_tag = getattr(session, "tag", None) or session.get("tag") or tag
        tasks.save(
            task_id, session_id=session_id, tag=used_tag, workspace=workspace, status="starting",
        )
        prompt = build_prompt(
            instructions=mailbox.get("instructions"),
            sender=body.get("from") or "",
            subject=body.get("subject") or "",
            attachment_paths=paths,
            plain=plain,
            raw_location=location,
        )
        if not wait_ready(sessions, session_id, timeout=ready_timeout, sleep=sleep):
            return fail("session was not ready for input")
        sessions.send(session_id, prompt.encode())
        tasks.save(task_id, status="started", sent_at=_now_iso(), error="")
        return "ack"
    except Exception as exc:
        return fail(exc)


def _materialize(body, mailbox, task_id, open_object):
    pointer = body.get("s3") or {}
    bucket = pointer.get("bucket")
    key = pointer.get("key")
    if not bucket or not key:
        return None, None, [], ""
    raw = open_object(bucket, key)
    if raw is None:
        return None, None, [], ""
    workspace = str(mailbox.get("workspace") or "")
    dest = Path(workspace) / ".dapier-mail" / task_id.replace(":", "_")
    try:
        plain, paths = parse_message(raw, dest)
    except OSError:
        return None, None, [], ""
    return raw, plain, paths, f"s3://{bucket}/{key}"


def _send_existing(body, mailbox, row, tasks, sessions, open_object, task_id, *, sleep, ready_timeout):
    """The session exists and the prompt was not sent. Do not start another."""
    _raw, plain, paths, location = _materialize(body, mailbox, task_id, open_object)
    if location == "" and not plain:
        tasks.save(task_id, error="the stored message could not be read", status="starting")
        return "retry"
    prompt = build_prompt(
        instructions=(mailbox or {}).get("instructions"),
        sender=body.get("from") or "",
        subject=body.get("subject") or "",
        attachment_paths=paths,
        plain=plain,
        raw_location=location or "unknown",
    )
    session_id = row["session_id"]
    if not wait_ready(sessions, session_id, timeout=ready_timeout, sleep=sleep):
        tasks.save(task_id, error="session was not ready for input", status="starting")
        return "retry"
    sessions.send(session_id, prompt.encode())
    tasks.save(task_id, status="started", sent_at=_now_iso(), error="")
    return "ack"


def receive_once(queue, queue_url):
    return queue.receive_message(
        QueueUrl=queue_url,
        WaitTimeSeconds=WAIT_SECONDS,
        MaxNumberOfMessages=1,
        VisibilityTimeout=VISIBILITY_SECONDS,
    )


def poll_once(queue, queue_url, **deps):
    """Long-poll once. Acknowledge only an ``ack``; a retry stays invisible until the timeout."""
    response = receive_once(queue, queue_url)
    for message in response.get("Messages") or []:
        try:
            body = json.loads(message.get("Body") or "null")
        except ValueError:
            body = None
        decision = handle_delivery(body, **deps) if isinstance(body, dict) else "ack"
        if decision == "ack":
            queue.delete_message(QueueUrl=queue_url, ReceiptHandle=message["ReceiptHandle"])
    return response


def serve(max_loops=None, queue=None, sessions=None):
    """Poll until interrupted. ``max_loops`` is for tests; production passes none."""
    queue_url = os.environ["AGENT_TASK_QUEUE_URL"]
    if queue is None:
        import boto3

        queue = boto3.client("sqs")
    if sessions is None:
        from aplexer import Client

        sessions = _AplexerSessions(Client(
            state_dir=os.environ.get("APLEXER_STATE_DIR") or None,
        ))
    from ..triggers import agent_mailboxes

    def load_mailbox(name):
        return agent_mailboxes.peek_mailbox(name)

    def load_senders():
        return agent_mailboxes.api_from_list()[1]["addresses"]

    def open_object(bucket, key):
        import boto3

        return boto3.client("s3").get_object(Bucket=bucket, Key=key)["Body"].read()

    tasks = TaskBook(agent_mailboxes.tasks_table())
    loops = 0
    while max_loops is None or loops < max_loops:
        poll_once(
            queue, queue_url,
            load_mailbox=load_mailbox, load_senders=load_senders,
            open_object=open_object, tasks=tasks, sessions=sessions,
        )
        loops += 1


class _AplexerSessions:
    """Adapt ``aplexer.Client`` to the start/status/send methods the consumer calls."""

    def __init__(self, client):
        self.client = client

    def start(self, *, workspace, tag, engine, profile, fresh):
        return self.client.start(
            workspace=workspace, tag=tag, engine=engine, profile=profile, fresh=fresh,
        )

    def status(self, selector):
        session = self.client.status(selector)
        return {"phase": getattr(session, "phase", None)}

    def send(self, selector, data):
        return self.client.send(selector, data)
