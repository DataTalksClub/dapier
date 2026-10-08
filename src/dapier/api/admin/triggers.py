"""Hook, schedule, and poll trigger endpoints."""
from ...triggers import email_triggers
from ...triggers import hook_triggers
from ... import http
import json
from ...triggers import poll_triggers
from ...triggers import schedule_triggers
from ...auth import session
from .. import hook_activity


def _hook_kind(event, body=None):
    """The hook trigger kind, from the query string or the request body."""
    query = event.get("queryStringParameters") or {}
    kind = (body or {}).get("kind") or query.get("kind") or "webhook"
    kind = str(kind).strip().lower()
    if kind not in hook_triggers.KINDS:
        raise email_triggers.TriggerError(
            f"hook kind must be one of: {', '.join(hook_triggers.KINDS)}")
    return kind

def list_hook_triggers(event):
    try:
        kind = _hook_kind(event)
    except email_triggers.TriggerError as exc:
        return http._json_response(400, {"error": str(exc)})
    status, payload = hook_triggers.api_list(kind=kind)
    hook_activity.attach_workflows(payload.get("hooks"))
    return http._json_response(status, payload)


def list_hook_deliveries(event, visible=None):
    """Recent webhook deliveries (the trigger inbox's webhook rows) with
    their run outcomes; ``hook`` narrows to one endpoint."""
    query = event.get("queryStringParameters") or {}
    status, payload = hook_activity.api_deliveries(
        query.get("hook"), query.get("limit", 25),
        next_token=query.get("next") or None, visible=visible)
    return http._json_response(status, payload)


def get_hook_delivery(delivery_id, visible=None):
    """One webhook delivery: request headers, payload, run outcomes."""
    status, payload = hook_activity.api_delivery(delivery_id, visible=visible)
    return http._json_response(status, payload)


def test_hook_trigger(event, operator):
    """Send test request: a sample (or the caller's) payload through the
    hook's real intake, signed with its own credential."""
    try:
        body = http._request_json(event)
    except (ValueError, json.JSONDecodeError) as exc:
        return http._json_response(400, {"error": str(exc) or "Invalid request"})
    status, payload = hook_activity.api_send_test(body.get("name"), body.get("data"))
    session._audit_event(str(body.get("name") or "unknown"), "hook-trigger.test", operator,
                         outcome="ok" if status == 200 else "error")
    return http._json_response(status, payload)

def save_hook_trigger(event, operator):
    try:
        body = http._request_json(event)
        kind = _hook_kind(event, body)
        status, payload = hook_triggers.api_save(body, operator, kind)
    except (ValueError, json.JSONDecodeError) as exc:
        return http._json_response(400, {"error": str(exc) or "Invalid request"})
    session._audit_event(payload.get("hook_id", "unknown"), "hook-trigger.save", operator,
                 outcome="created" if payload.get("created") else "updated")
    return http._json_response(status, payload)

def delete_hook_trigger(event, operator):
    try:
        kind = _hook_kind(event)
        query = event.get("queryStringParameters") or {}
        status, payload = hook_triggers.api_delete(
            query.get("name", ""), operator, kind=kind)
    except email_triggers.TriggerError as exc:
        return http._json_response(404, {"error": str(exc)})
    session._audit_event(payload.get("hook_id", "unknown"), "hook-trigger.delete", operator, outcome="deleted")
    return http._json_response(status, payload)

def list_schedule_triggers(event):
    status, payload = schedule_triggers.api_list()
    return http._json_response(status, payload)

def save_schedule_trigger(event, operator):
    try:
        body = http._request_json(event)
        status, payload = schedule_triggers.api_save(body, operator)
    except (ValueError, json.JSONDecodeError) as exc:
        return http._json_response(400, {"error": str(exc) or "Invalid request"})
    session._audit_event(payload.get("schedule_id", "unknown"), "schedule-trigger.save", operator,
                 outcome="created" if payload.get("created") else "updated")
    return http._json_response(status, payload)

def delete_schedule_trigger(event, operator):
    try:
        query = event.get("queryStringParameters") or {}
        status, payload = schedule_triggers.api_delete(query.get("name", ""), operator)
    except email_triggers.TriggerError as exc:
        return http._json_response(404, {"error": str(exc)})
    session._audit_event(payload.get("schedule_id", "unknown"), "schedule-trigger.delete",
                 operator, outcome="deleted")
    return http._json_response(status, payload)

def upcoming_schedule_triggers(event):
    query = event.get("queryStringParameters") or {}
    status, payload = schedule_triggers.api_upcoming(query.get("hours", 24))
    return http._json_response(status, payload)

def run_schedule_trigger(name, operator):
    """Run now: one manual fire through the event queue."""
    try:
        status, payload = schedule_triggers.api_run_now(name, operator)
    except email_triggers.TriggerError as exc:
        return http._json_response(404, {"error": str(exc)})
    session._audit_event(payload.get("schedule_id", "unknown"), "schedule-trigger.run",
                 operator, outcome="ok")
    return http._json_response(status, payload)

def set_schedule_trigger_enabled(name, enabled, operator):
    """Pause or resume a schedule (its EventBridge rule state)."""
    try:
        status, payload = schedule_triggers.api_set_enabled(name, enabled, operator)
    except email_triggers.TriggerError as exc:
        return http._json_response(404, {"error": str(exc)})
    session._audit_event(payload.get("schedule_id", "unknown"),
                 "schedule-trigger.resume" if enabled else "schedule-trigger.pause",
                 operator, outcome="ok")
    return http._json_response(status, payload)

def list_poll_triggers(event):
    status, payload = poll_triggers.api_list()
    return http._json_response(status, payload)

def save_poll_trigger(event, operator):
    try:
        body = http._request_json(event)
        status, payload = poll_triggers.api_save(body, operator)
    except (ValueError, json.JSONDecodeError) as exc:
        return http._json_response(400, {"error": str(exc) or "Invalid request"})
    session._audit_event(payload.get("poll_id", "unknown"), "poll-trigger.save", operator,
                 outcome="created" if payload.get("created") else "updated")
    return http._json_response(status, payload)

def delete_poll_trigger(event, operator):
    try:
        query = event.get("queryStringParameters") or {}
        status, payload = poll_triggers.api_delete(query.get("name", ""), operator)
    except email_triggers.TriggerError as exc:
        return http._json_response(404, {"error": str(exc)})
    session._audit_event(payload.get("poll_id", "unknown"), "poll-trigger.delete",
                 operator, outcome="deleted")
    return http._json_response(status, payload)


def _poll_query(event):
    return event.get("queryStringParameters") or {}

def show_poll_trigger(event, name):
    try:
        status, payload = poll_triggers.api_show(name)
    except email_triggers.TriggerError as exc:
        return http._json_response(404, {"error": str(exc)})
    return http._json_response(status, payload)

def poll_activity(event):
    query = _poll_query(event)
    try:
        status, payload = poll_triggers.api_activity(query.get("name"), query.get("limit", 25))
    except email_triggers.TriggerError as exc:
        return http._json_response(404, {"error": str(exc)})
    return http._json_response(status, payload)

def poll_trigger_action(event, name, action, operator):
    """Poll now (check), pause, resume, and reset for one poll — the same
    poll_triggers calls `dapier polls check|pause|resume|reset` reach."""
    try:
        if action == "check":
            status, payload = poll_triggers.api_check(name)
        elif action in ("pause", "resume"):
            status, payload = poll_triggers.api_set_enabled(name, action == "resume", operator)
        else:
            status, payload = poll_triggers.api_reset(name, http._request_json(event), operator)
    except email_triggers.TriggerError as exc:
        code = 404 if str(exc).startswith("no poll trigger") else 400
        return http._json_response(code, {"error": str(exc)})
    except (ValueError, json.JSONDecodeError) as exc:
        return http._json_response(400, {"error": str(exc) or "Invalid request"})
    except RuntimeError as exc:
        return http._json_response(502, {"error": str(exc) or "poll fetch failed"})
    session._audit_event(name, f"poll-trigger.{action}", operator,
                         outcome="ok" if status < 300 else "error")
    return http._json_response(status, payload)
