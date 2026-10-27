"""Durable Hub completion delivery through the existing poll framework."""
import json
import logging
import re
import time
from uuid import uuid4
from datetime import datetime

from boto3.dynamodb.conditions import Key
from botocore.exceptions import ClientError

from src.dapier.engine.actions import storage
from src.dapier.triggers.email_triggers import TriggerError

from .notifications import send_notification
from .session import DynamoJournal

logger = logging.getLogger(__name__)
SYSTEM_SCOPE = "system:hub-sessions"
POLL_ID = "hub-session-completions"
PENDING_PREFIX = "hub-session/pending/"
PAGE_SIZE = 5
REPORT_GRACE_SECONDS = 300
TERMINAL = frozenset({"stopped", "failed", "refused", "cancelled"})


class CompletionStore:
    def __init__(self, table=None):
        self.table = table if table is not None else storage._table()
        self.journal = DynamoJournal(self.table, SYSTEM_SCOPE)

    def enqueue(self, metadata):
        sid = metadata["hub_session_id"]
        if self.journal.get("done/" + sid) is None:
            self.journal.once("pending/" + sid, metadata)

    def page(self, cursor=None):
        query = {"KeyConditionExpression": Key("scope").eq(SYSTEM_SCOPE) & Key("key").begins_with(PENDING_PREFIX),
                 "Limit": PAGE_SIZE, "ConsistentRead": True}
        if cursor:
            try:
                key = json.loads(cursor)
                if set(key) != {"scope", "key"} or key["scope"] != SYSTEM_SCOPE or not key["key"].startswith(PENDING_PREFIX):
                    raise ValueError
            except (ValueError, TypeError, AttributeError):
                raise ValueError("invalid Hub completion cursor") from None
            query["ExclusiveStartKey"] = key
        page = self.table.query(**query)
        # Empty string deliberately parks a reset cursor. None would leave the
        # previous page's cursor in place and starve jobs before it forever.
        next_cursor = json.dumps(page["LastEvaluatedKey"], sort_keys=True) if page.get("LastEvaluatedKey") else ""
        return [json.loads(row["value"]) for row in page.get("Items", [])], next_cursor

    def pending(self, sid):
        return self.journal.get("pending/" + sid)

    def result(self, sid):
        return self.journal.get("result/" + sid)

    def event_delivered(self, sid):
        return self.journal.get("event/" + sid) is not None

    def _notification_key(self, sid):
        return {"scope": SYSTEM_SCOPE, "key": "hub-notification/" + sid}

    def notification_done(self, sid):
        row = self.table.get_item(Key=self._notification_key(sid), ConsistentRead=True).get("Item") or {}
        return row.get("status") in {"sent", "not_requested"}

    def no_notification(self, sid):
        try:
            self.table.put_item(Item={**self._notification_key(sid), "status": "not_requested"},
                                ConditionExpression="attribute_not_exists(#k)", ExpressionAttributeNames={"#k": "key"})
        except ClientError as exc:
            if exc.response["Error"]["Code"] != "ConditionalCheckFailedException":
                raise

    def claim_notification(self, sid, *, now=None):
        now = int(time.time()) if now is None else now
        owner = str(uuid4())
        try:
            self.table.update_item(
                Key=self._notification_key(sid),
                UpdateExpression="SET #s = :sending, #until = :until, #owner = :owner",
                ConditionExpression="attribute_not_exists(#s) OR (#s <> :sent AND #s <> :none AND (attribute_not_exists(#until) OR #until <= :now))",
                ExpressionAttributeNames={"#s": "status", "#until": "lease_until", "#owner": "lease_owner"},
                ExpressionAttributeValues={":sending": "sending", ":sent": "sent", ":none": "not_requested",
                                           ":now": now, ":until": now + 120, ":owner": owner})
            return owner
        except ClientError as exc:
            if exc.response["Error"]["Code"] != "ConditionalCheckFailedException":
                raise
            return None

    def mark_notified(self, sid, owner, message_id):
        if not message_id:
            raise RuntimeError("SES did not acknowledge the completion email")
        self.table.update_item(
            Key=self._notification_key(sid),
            UpdateExpression="SET #s = :sent, message_id = :message",
            ConditionExpression="#owner = :owner AND #s = :sending",
            ExpressionAttributeNames={"#s": "status", "#owner": "lease_owner"},
            ExpressionAttributeValues={":sent": "sent", ":sending": "sending", ":owner": owner, ":message": str(message_id)})

    def finish_if_delivered(self, sid):
        if self.event_delivered(sid) and self.notification_done(sid):
            self.journal.once("done/" + sid, {"event_delivered": True, "notification_complete": True})
            self.table.delete_item(Key={"scope": SYSTEM_SCOPE, "key": PENDING_PREFIX + sid})


def completion_for(metadata, task, *, now=None):
    """Use only this session's canonical state and Hub-authored fleet report."""
    if not isinstance(task, dict) or task.get("id") != metadata["hub_task_id"]:
        raise RuntimeError("Hub completion returned a different task")
    sid = metadata["hub_session_id"]
    session = next((s for s in task.get("assigned_sessions", []) if s.get("hub_session_id") == sid), None)
    if session is None or session.get("linkage") != "hub_session":
        raise RuntimeError("Hub completion session is not visible to the service identity")
    state = session.get("state")
    if state not in TERMINAL:
        return None
    launch_id = session.get("fleet_launch_id")
    reports = (task.get("payload_data") or {}).get("history") or []
    report = next((r for r in reversed(reports) if
                   r.get("kind") == "report" and r.get("actor") == "hub:service:fleet-finish"
                   and r.get("authority") == "hub_launch_verified"
                   and (r.get("source") or {}).get("reference") == f"fleet-finish:{launch_id}"), None) if launch_id else None
    if launch_id and report is None:
        # Hub stores timezone-aware ISO timestamps. Report append is
        # best-effort; cancelled/expired launches may never get one.
        updated = session.get("updated_at")
        try:
            stamp = datetime.fromisoformat(updated.replace("Z", "+00:00"))
            updated_at = stamp.timestamp() if stamp.tzinfo is not None else None
        except (ValueError, TypeError, AttributeError):
            updated_at = None
        now = time.time() if now is None else now
        if updated_at is not None and now - updated_at < REPORT_GRACE_SECONDS:
            return None
    fleet_state = worker_status = None
    if report:
        text = report.get("text")
        if not isinstance(text, str) or not text.strip():
            raise RuntimeError("Hub finish report has no text")
        # This is Hub's bounded canonical header, not the worker's free text.
        # If Hub changes its presentation, keep status unknown and carry the
        # intact report rather than guessing success from arbitrary prose.
        header = re.match(r"^Hub session " + re.escape(sid) +
                          r" finished \((exited|failed)(?:/(done|failed|waiting-on-human|stuck))?, launch " +
                          re.escape(launch_id) + r"\)\.", text)
        if header:
            fleet_state, worker_status = header.groups()
    return {"id": sid, "hub_task_id": metadata["hub_task_id"], "hub_session_id": sid,
            "hub_link": metadata["hub_link"], "state": state, "fleet_state": fleet_state,
            "worker_report_status": worker_status, "finish_report": report,
            "finish_report_status": "available" if report else "unavailable" if launch_id else "not_launched",
            "source_workflow_id": metadata["source_workflow_id"],
            "source_event_id": metadata["source_event_id"], "step_id": metadata["step_id"]}


def validate(body):
    if str(body.get("name") or "").strip().lower() != POLL_ID:
        raise TriggerError(f"Hub completions use the single poll named {POLL_ID}")
    if body.get("cursor_mode") not in (None, "", "next_cursor"):
        raise TriggerError("Hub completions require next_cursor mode")
    if body.get("id_path") not in (None, "", "id"):
        raise TriggerError("Hub completions identify items by session id")
    return {"cursor_mode": "next_cursor", "id_path": "id", "max_items": PAGE_SIZE}


def fetch(item, cursor=None, *, store=None, get_task=None):
    """Read-only, including when called by sampling or cursor reset."""
    if item.get("poll_id") != POLL_ID:
        raise RuntimeError(f"Hub completions require poll {POLL_ID}")
    if get_task is None:
        from .action import get_hub_task
        get_task = get_hub_task
    store = store if store is not None else CompletionStore()
    pending, next_cursor = store.page(cursor)
    results = []
    for metadata in pending:
        result = store.result(metadata["hub_session_id"])
        if result is None:
            try:
                result = completion_for(metadata, get_task(metadata["hub_task_id"]))
            except Exception as exc:
                # Leave inaccessible jobs pending without blocking the scan.
                logger.warning("Hub completion read pending", extra={
                    "hub_session_id": metadata["hub_session_id"], "error_type": type(exc).__name__})
                continue
        if result is not None:
            results.append(result)
    return results, next_cursor


def prepare(item, items, *, store=None, send=None):
    """Execution-only notification phase; errors never suppress fresh events."""
    store = store if store is not None else CompletionStore()
    send = send if send is not None else send_notification
    errors = False
    retained = []
    for raw in items:
        sid = raw["hub_session_id"]
        try:
            snapshot = store.journal.once("result/" + sid, raw)
            if not isinstance(snapshot, dict) or snapshot.get("hub_session_id") != sid:
                raise RuntimeError("invalid stored Hub completion result")
            raw.clear()
            raw.update(snapshot)
        except Exception as exc:
            logger.warning("Hub completion snapshot pending", extra={
                "hub_session_id": sid, "error_type": type(exc).__name__})
            errors = True
            continue  # No publication or email before the durable snapshot.
        try:
            metadata = store.pending(sid)
            if metadata:
                if not metadata.get("notify_to"):
                    store.no_notification(sid)
                elif not store.notification_done(sid):
                    owner = store.claim_notification(sid)
                    if owner:
                        store.mark_notified(sid, owner, send(metadata, snapshot))
                    if not store.notification_done(sid):
                        errors = True
        except Exception as exc:
            # Only exception class and session id: no credential, HTTP body,
            # report text or recipient goes into logs or monitor errors.
            logger.warning("Hub completion delivery pending", extra={"hub_session_id": sid, "error_type": type(exc).__name__})
            errors = True
        # Notification errors must not bypass the durable event receipt.
        # Otherwise an email retry beyond the poll seen-set TTL could emit
        # the same completion again.
        if not store.event_delivered(sid):
            retained.append(raw)
        try:
            store.finish_if_delivered(sid)
        except Exception as exc:
            logger.warning("Hub completion cleanup pending", extra={"hub_session_id": sid, "error_type": type(exc).__name__})
            errors = True
    items[:] = retained
    return "Hub completion notification or delivery bookkeeping is pending" if errors else None


def ack(item, raw, event_id, *, store=None):
    store = store if store is not None else CompletionStore()
    sid = raw["hub_session_id"]
    store.journal.once("event/" + sid, {"event_id": event_id})
    store.finish_if_delivered(sid)
