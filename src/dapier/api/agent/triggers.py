"""Trigger endpoints: samples and discovery, the inbound sender
allow-list, and email/hook/schedule/poll trigger management."""

import json

from ...connectors import trigger_discovery
from .. import runs
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


__all__ = ["discover_samples_api", "email_from_api", "email_triggers_api", "hook_triggers_api", "poll_triggers_api", "schedule_triggers_api", "trigger_sample_api"]



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
    """Operator-only trigger management over the CLI's bearer authentication."""
    subject, error = require_operator(event, "email-trigger")
    if error:
        return error
    table_ref = email_triggers.get_table()
    try:
        if method == "GET":
            status, payload = email_triggers.api_list(table_ref)
        elif method == "PUT":
            body = json.loads(event.get("body") or "{}")
            status, payload = email_triggers.api_save(body, subject, table_ref=table_ref)
        else:
            query = event.get("queryStringParameters") or {}
            status, payload = email_triggers.api_delete(
                query.get("name", ""), subject, table_ref=table_ref,
            )
    except (ValueError, json.JSONDecodeError) as exc:
        return _json_response(400, {"error": str(exc) or "Invalid request"})
    audit.emit(payload.get("name", "unknown"), "email-trigger", subject,
               outcome="ok" if status == 200 else "error")
    return _json_response(status, payload)


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
