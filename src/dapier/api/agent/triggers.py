"""Trigger endpoints: samples and discovery, the inbound sender
allow-list, and email/hook/schedule/poll trigger management."""

import json

from ...connectors import trigger_discovery
from .. import hook_activity, runs
from ...triggers import email_from, email_triggers, hook_triggers, poll_triggers, schedule_triggers

from .common import _json_response, _no_store, _visibility

from .common import _LateBinding

# Shared dependencies resolved through the agent package at call time:
# tests patch agent.<name> and every route module must see the patch.
authenticate = _LateBinding("authenticate")
require_operator = _LateBinding("require_operator")
_is_operator = _LateBinding("_is_operator")
_tables = _LateBinding("_tables")
audit = _LateBinding("audit")
verify_id_token = _LateBinding("verify_id_token")


__all__ = ["discover_samples_api", "email_from_api", "email_triggers_api", "hook_delivery_api", "hook_test_api", "hook_triggers_api", "poll_activity_api", "poll_trigger_action_api", "poll_trigger_show_api", "poll_triggers_api", "schedule_action_api", "schedule_triggers_api", "schedule_upcoming_api", "trigger_sample_api"]



def discover_samples_api(event):
    """Operator-only trigger sample pull: a realistic event for one connector.

    Zapier's 'pull in sample data', for the CLI: the same dispatch the
    console's discover route shares (connectors.trigger_discovery.api_discover),
    so a pulled sample is exactly what the designer's test panel fills in.
    """
    subject, error = require_operator(event, "triggers.sample")
    if error:
        return error
    try:
        body = json.loads(event.get("body") or "{}")
    except (ValueError, json.JSONDecodeError) as exc:
        return _json_response(400, {"error": str(exc) or "Invalid request"})
    status, payload = trigger_discovery.api_discover(body)
    audit.emit(str((body or {}).get("connector") or payload.get("connector") or "unknown"),
               "triggers.sample", subject,
               outcome="ok" if status == 200 else "error", error=payload.get("error"))
    return _json_response(status, payload)


def trigger_sample_api(event):
    """Operator-only trigger sample for one workflow: the newest run's
    recorded trigger input, else the connector's discovery sample.

    The CLI twin of the console's GET /api/admin/triggers/sample — both
    dispatch through runs.api_trigger_sample, so `dapier triggers sample
    --workflow` autofills from exactly what the designer's inspector offers.
    """
    subject, error = require_operator(event, "triggers.sample")
    if error:
        return error
    query = event.get("queryStringParameters") or {}
    status, payload = runs.api_trigger_sample(
        query.get("workflow") or query.get("workflow_id"),
        visible=_visibility(event, subject))
    audit.emit(str(query.get("workflow") or query.get("workflow_id") or "unknown"),
               "triggers.sample", subject,
               outcome="ok" if status == 200 else "error", error=payload.get("error"))
    return _no_store(_json_response(status, payload))


def email_from_api(event, method):
    subject, error = require_operator(event, "email-from")
    if error:
        return error
    try:
        if method == "GET":
            status, payload = email_from.api_list()
        elif method == "POST":
            body = json.loads(event.get("body") or "{}")
            status, payload = email_from.api_add((body or {}).get("address"))
        else:
            query = event.get("queryStringParameters") or {}
            status, payload = email_from.api_remove(query.get("address", ""))
    except (email_from.FromError, ValueError, json.JSONDecodeError) as exc:
        return _json_response(400, {"error": str(exc) or "Invalid request"})
    audit.emit("email-from", "email-from", subject, outcome="ok" if status == 200 else "error")
    return _json_response(status, payload)


def email_triggers_api(event, method):
    """Operator-only email inventory over the CLI's bearer authentication."""
    subject, error = require_operator(event, "email-trigger")
    if error:
        return error
    from ...triggers.email_routes import inventory
    payload = inventory()
    audit.emit("email-triggers", "email-trigger", subject, outcome="ok")
    return _json_response(200, payload)


def hook_triggers_api(event, method):
    """Operator-only webhook/Telegram trigger management over the CLI's bearer authentication."""
    subject, error = require_operator(event, "hook-trigger")
    if error:
        return error
    table_ref = hook_triggers.get_table()
    try:
        if method == "GET":
            query = event.get("queryStringParameters") or {}
            kind = str(query.get("kind", "") or "").strip().lower() or None
            if kind and kind not in hook_triggers.KINDS:
                raise ValueError(f"hook kind must be one of: {', '.join(hook_triggers.KINDS)}")
            status, payload = hook_triggers.api_list(table_ref, kind=kind)
            hook_activity.attach_workflows(payload.get("hooks"))
        elif method == "PUT":
            body = json.loads(event.get("body") or "{}")
            kind = str((body or {}).get("kind") or "webhook").strip().lower()
            if kind not in hook_triggers.KINDS:
                raise ValueError(f"hook kind must be one of: {', '.join(hook_triggers.KINDS)}")
            status, payload = hook_triggers.api_save(
                body, subject, kind, table_ref=table_ref,
                connections_table=_tables()[0],
            )
        else:
            query = event.get("queryStringParameters") or {}
            kind = str(query.get("kind", "") or "").strip().lower() or None
            status, payload = hook_triggers.api_delete(
                query.get("name", ""), subject, kind=kind, table_ref=table_ref,
                connections_table=_tables()[0],
            )
    except (ValueError, json.JSONDecodeError) as exc:
        return _json_response(400, {"error": str(exc) or "Invalid request"})
    audit.emit(payload.get("hook_id", "unknown"), "hook-trigger", subject,
               outcome="ok" if status == 200 else "error")
    return _json_response(status, payload)


def schedule_triggers_api(event, method):
    """Operator-only cron/rate schedule trigger management over the CLI's bearer authentication."""
    subject, error = require_operator(event, "schedule-trigger")
    if error:
        return error
    table_ref = schedule_triggers.get_table()
    try:
        if method == "GET":
            status, payload = schedule_triggers.api_list(table_ref)
        elif method == "PUT":
            body = json.loads(event.get("body") or "{}")
            status, payload = schedule_triggers.api_save(body, subject, table_ref=table_ref)
        else:
            query = event.get("queryStringParameters") or {}
            status, payload = schedule_triggers.api_delete(
                query.get("name", ""), subject, table_ref=table_ref,
            )
    except (ValueError, json.JSONDecodeError) as exc:
        return _json_response(400, {"error": str(exc) or "Invalid request"})
    audit.emit(payload.get("schedule_id", "unknown"), "schedule-trigger", subject,
               outcome="ok" if status == 200 else "error")
    return _json_response(status, payload)


def schedule_upcoming_api(event):
    """Operator-only: every enabled schedule's fires in the next N hours."""
    subject, error = require_operator(event, "schedule-trigger")
    if error:
        return error
    query = event.get("queryStringParameters") or {}
    status, payload = schedule_triggers.api_upcoming(query.get("hours", 24))
    return _json_response(status, payload)


def schedule_action_api(event, name, action):
    """Operator-only Run now / Pause / Resume for one schedule — the CLI
    twin of the console's buttons (same schedule_triggers functions)."""
    subject, error = require_operator(event, "schedule-trigger")
    if error:
        return error
    try:
        if action == "run":
            status, payload = schedule_triggers.api_run_now(name, subject)
        else:
            status, payload = schedule_triggers.api_set_enabled(
                name, action == "resume", subject)
    except email_triggers.TriggerError as exc:
        return _json_response(404, {"error": str(exc)})
    audit.emit(payload.get("schedule_id", "unknown"), f"schedule-trigger.{action}", subject,
               outcome="ok")
    return _json_response(status, payload)


def poll_triggers_api(event, method):
    """Operator-only poll trigger management over the CLI's bearer authentication."""
    subject, error = require_operator(event, "poll-trigger")
    if error:
        return error
    table_ref = poll_triggers.get_table()
    try:
        if method == "GET":
            status, payload = poll_triggers.api_list(table_ref)
        elif method == "PUT":
            body = json.loads(event.get("body") or "{}")
            status, payload = poll_triggers.api_save(body, subject, table_ref=table_ref)
        else:
            query = event.get("queryStringParameters") or {}
            status, payload = poll_triggers.api_delete(
                query.get("name", ""), subject, table_ref=table_ref,
            )
    except (ValueError, json.JSONDecodeError) as exc:
        return _json_response(400, {"error": str(exc) or "Invalid request"})
    audit.emit(payload.get("poll_id", "unknown"), "poll-trigger", subject,
               outcome="ok" if status == 200 else "error")
    return _json_response(status, payload)


def hook_delivery_api(event, delivery_id=None):
    """Operator-only webhook delivery log: recent deliveries (``hook``
    narrows to one endpoint) or, with an id, one delivery's request
    headers, payload and run outcomes — the console Hooks tab's data."""
    subject, error = require_operator(event, "hook-trigger.deliveries")
    if error:
        return error
    visible = _visibility(event, subject)
    if delivery_id:
        status, payload = hook_activity.api_delivery(delivery_id, visible=visible)
        return _no_store(_json_response(status, payload))
    query = event.get("queryStringParameters") or {}
    status, payload = hook_activity.api_deliveries(
        query.get("hook"), query.get("limit", 25),
        next_token=query.get("next") or None, visible=visible)
    return _no_store(_json_response(status, payload))


def hook_test_api(event):
    """Operator-only Send test request: a sample (or the caller's) payload
    through the hook's real intake, signed with its own credential."""
    subject, error = require_operator(event, "hook-trigger.test")
    if error:
        return error
    try:
        body = json.loads(event.get("body") or "{}")
        if not isinstance(body, dict):
            raise ValueError("request body must be an object")
    except (ValueError, json.JSONDecodeError) as exc:
        return _json_response(400, {"error": str(exc) or "Invalid request"})
    status, payload = hook_activity.api_send_test(body.get("name"), body.get("data"))
    audit.emit(str(body.get("name") or "unknown"), "hook-trigger.test", subject,
               outcome="ok" if status == 200 else "error")
    return _no_store(_json_response(status, payload))


def poll_trigger_show_api(event, name):
    """Operator-only: one poll's monitor view (health, last checks, workflows)."""
    subject, error = require_operator(event, "poll-trigger")
    if error:
        return error
    try:
        status, payload = poll_triggers.api_show(name)
    except email_triggers.TriggerError as exc:
        return _json_response(404, {"error": str(exc)})
    return _no_store(_json_response(status, payload))


def poll_activity_api(event):
    """Operator-only: items polls picked up (from the trigger inbox) with
    the runs they started — `dapier polls activity`."""
    subject, error = require_operator(event, "poll-trigger")
    if error:
        return error
    query = event.get("queryStringParameters") or {}
    try:
        status, payload = poll_triggers.api_activity(query.get("name"), query.get("limit", 25))
    except email_triggers.TriggerError as exc:
        return _json_response(404, {"error": str(exc)})
    return _no_store(_json_response(status, payload))


def poll_trigger_action_api(event, name, action):
    """Operator-only Poll now / pause / resume / reset — the CLI twin of the
    console's POST /api/admin/poll-triggers/<name>/<action>."""
    subject, error = require_operator(event, "poll-trigger")
    if error:
        return error
    try:
        if action == "check":
            status, payload = poll_triggers.api_check(name)
        elif action in ("pause", "resume"):
            status, payload = poll_triggers.api_set_enabled(name, action == "resume", subject)
        else:
            body = json.loads(event.get("body") or "{}")
            status, payload = poll_triggers.api_reset(name, body, subject)
    except email_triggers.TriggerError as exc:
        code = 404 if str(exc).startswith("no poll trigger") else 400
        return _json_response(code, {"error": str(exc)})
    except (ValueError, json.JSONDecodeError) as exc:
        return _json_response(400, {"error": str(exc) or "Invalid request"})
    except RuntimeError as exc:
        return _json_response(502, {"error": str(exc) or "poll fetch failed"})
    audit.emit(name, f"poll-trigger.{action}", subject,
               outcome="ok" if status < 300 else "error")
    return _json_response(status, payload)
