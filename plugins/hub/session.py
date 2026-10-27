"""Hub session request protocol, independent of credential provisioning.

The action runner supplies authenticated HTTP and durable storage. This
module does not resolve secrets. No agent process runs in Dapier.
"""
import hashlib
import json
import re
from pathlib import PurePosixPath
from uuid import UUID

from src.dapier.engine.actions.templating import render


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _required(value, field, limit):
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"hub_session requires non-empty {field}")
    value = value.strip()
    if len(value.encode("utf-8")) > limit:
        raise ValueError(f"hub_session {field} exceeds {limit} bytes")
    return value


def request_key(workflow_id, event_id, step_id):
    """Identity is a tuple, so delimiters in ids cannot collide."""
    parts = [_required(v, k, 2048) for k, v in (
        ("workflow id", workflow_id), ("event id", event_id), ("step id", step_id))]
    return "dapier-session-" + hashlib.sha256(_json(parts).encode()).hexdigest()


def build_request(action, event, workflow_id, steps=None):
    """Render and validate once; reject oversized input rather than truncate."""
    if "engine" in action:
        raise ValueError("hub_session uses workspace and tier; engine is not supported")
    if action.get("attachments"):
        raise ValueError("hub_session attachment delivery is not configured yet")
    key = request_key(workflow_id, event.get("id"), action.get("id"))

    def field(name, limit, default=None):
        raw = action.get(name, default)
        if not isinstance(raw, str):
            raise ValueError(f"hub_session requires string {name}")
        return _required(render(raw, event, steps), name, limit)

    title = field("title", 256)
    text = field("text", 8000)
    criteria = field("acceptance_criteria", 8000)
    project = field("project", 128, "dapier-agents")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}", project):
        raise ValueError("hub_session project must be a Hub identifier")
    workspace = field("workspace", 128, "dapier-agents")
    tier = field("tier", 10, "daily")
    if tier not in {"quick", "daily", "deep"}:
        raise ValueError("hub_session tier must be quick, daily or deep")
    brief = f"{title}\n\n{text}\n\nAcceptance criteria:\n{criteria}"
    _required(brief, "brief", 8192)
    session = {"workspace": workspace, "tier": tier, "mode": "headless", "brief": brief}
    if action.get("cwd"):
        cwd = field("cwd", 1024)
        path = PurePosixPath(cwd)
        if path.is_absolute() or ".." in path.parts or "\\" in cwd or "\x00" in cwd:
            raise ValueError("hub_session cwd must stay within the Hub workspace")
        session["cwd"] = cwd
    from .notifications import configuration

    return {
        "create": {"key": key, "project": project, "title": title, "text": text,
                   "acceptance_criteria": criteria,
                   "source": {"type": "forwarded", "reference": key, "timestamp": None}},
        "session": session,
        "completion": configuration(action, event, workflow_id, title=title, steps=steps),
    }


class DynamoJournal:
    """Immutable phase snapshots in Dapier's existing workflow storage table.

    Conditional inserts choose one winner across concurrent retries. Strong
    reads recover the winner. No TTL: expiry would permit duplicate launches.
    Records contain prompts, so callers must use the workflow's own scope.
    """
    def __init__(self, table, workflow_id):
        self.table = table
        self.scope = _required(workflow_id, "workflow id", 2048)

    def once(self, key, value):
        from botocore.exceptions import ClientError

        primary = {"scope": self.scope, "key": "hub-session/" + key}
        stored = self.get(key)
        if stored is not None:
            return stored
        value = value() if callable(value) else value
        item = {**primary, "value": _json(value)}
        try:
            self.table.put_item(Item=item, ConditionExpression="attribute_not_exists(#k)",
                                ExpressionAttributeNames={"#k": "key"})
            return json.loads(item["value"])
        except ClientError as exc:
            if exc.response["Error"]["Code"] != "ConditionalCheckFailedException":
                raise
        stored = self.table.get_item(Key=primary, ConsistentRead=True).get("Item")
        if not stored:
            raise RuntimeError("hub_session journal changed during retry")
        return json.loads(stored["value"])

    def get(self, key):
        stored = self.table.get_item(
            Key={"scope": self.scope, "key": "hub-session/" + key},
            ConsistentRead=True).get("Item")
        return json.loads(stored["value"]) if stored else None


class HubSessionError(RuntimeError):
    """Safe error details: never include HTTP bodies containing task data."""


def _response(status, body, phase):
    if not isinstance(status, int) or not 200 <= status < 300:
        # Only Hub's bounded machine code is safe to expose as an error.
        code = body.get("error") if isinstance(body, dict) else None
        code = code if isinstance(code, str) and re.fullmatch(r"[a-z_]{1,80}", code) else "request_failed"
        raise HubSessionError(f"Hub {phase}: HTTP {status}, {code}")
    if not isinstance(body, dict):
        raise HubSessionError(f"Hub {phase}: invalid response")
    return body


def _uuid(value, field):
    try:
        return str(UUID(value))
    except (ValueError, TypeError, AttributeError):
        raise HubSessionError(f"Hub returned invalid {field}") from None


def launch(action, event, workflow_id, *, journal, post, steps=None):
    """Create and assign using a caller-owned authenticated ``post(path, body)``.

    Each HTTP mutation has its exact payload journaled before it is sent.
    Ambiguous timeouts propagate; retry replays the same Hub keys/payloads.
    Assignment is not evidence that a native worker is already running.
    """
    key = request_key(workflow_id, event.get("id"), action.get("id"))
    completed = journal.get(key + "/assigned")
    if completed is not None:
        return completed
    request = journal.once(key + "/request", lambda: build_request(action, event, workflow_id, steps))
    created = _response(*post("", request["create"]), "create")
    task = created.get("task") or {}
    task_id = _uuid(task.get("id"), "task id")
    version = task.get("version")
    if isinstance(version, bool) or not isinstance(version, int) or version < 1:
        raise HubSessionError("Hub returned invalid task version")
    assignment = journal.once(key + "/assign", {
        "task_id": task_id,
        "payload": {"key": key + "-assign", "expected_version": version,
                    "session": request["session"]},
    })
    if assignment["task_id"] != task_id:
        raise HubSessionError("Hub create replay changed task identity")
    assigned = _response(*post(f"/{task_id}/assign", assignment["payload"]), "assign")
    session = assigned.get("assigned_session") or {}
    session_id = _uuid(session.get("hub_session_id"), "session id")
    # Keep held/uncertain results explicit; retries never choose a fresh key.
    result = {"hub_task_id": task_id, "hub_session_id": session_id,
              "hub_link": f"https://hub.dtcdev.click/#/intake/{task_id}",
              "state": session.get("state") or "unknown",
              "fleet_launch_id": session.get("fleet_launch_id")}
    if "hold" in assigned:
        result["hold"] = assigned["hold"]
    return journal.once(key + "/assigned", result)
