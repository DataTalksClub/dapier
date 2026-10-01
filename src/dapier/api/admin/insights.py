"""Usage, quotas, error digests, and the audit trail reads."""
from ... import audit as audit_log
from ... import error_digest
from .. import errors as errors_api
from ... import http
import json
from ...auth import session
from ...engine import usage as usage_rollup


def usage(event, visible=None):
    """Task usage rollup: tasks per workflow per month, latest months first.

    ``visible`` (G17 auth.visibility) read-filters the per-workflow rows for
    non-operators; the quota endpoint (account-wide) is separate."""
    query = event.get("queryStringParameters") or {}
    status, payload = usage_rollup.api_usage(query.get("months", 12), visible=visible)
    return http._json_response(status, payload)


def quota_get(event):
    """The monthly task quota: limit, tasks used and left this month."""
    status, payload = usage_rollup.api_quota_get()
    return http._json_response(status, payload)


def quota_save(event, operator):
    """Store or clear the monthly task budget ({"limit": 1000} or
    {"limit": "off"}), audited like every other settings write."""
    try:
        body = http._request_json(event)
    except (ValueError, json.JSONDecodeError):
        return http._json_response(400, {"error": "Invalid request"})
    if not isinstance(body, dict):
        return http._json_response(400, {"error": "Invalid request"})
    status, payload = usage_rollup.api_quota_set(body.get("limit"))
    if status == 200:
        session._audit_event("usage", "quota.set", operator or "unknown",
                             outcome="ok")
    return http._json_response(status, payload)


def errors_summary(event, visible=None):
    """Failed-run counts by workflow over the recent window (default 7 days).

    ``visible`` (G17 auth.visibility) scopes the counts like the runs list:
    a non-operator keeps its own workflows' failures."""
    query = event.get("queryStringParameters") or {}
    status, payload = errors_api.api_summary(query.get("days", 7),
                                             visible=visible)
    return http._json_response(status, payload)


def send_error_digest(event, operator):
    """Render-and-send the operator error digest now.

    The same domain function the daily ErrorDigestFunction schedule runs;
    the response reports what was sent, or ``skipped`` when nothing failed
    in the window (no noise email).
    """
    payload = error_digest.send()
    if payload.get("sent"):
        session._audit_event("errors", "errors.send-digest",
                             operator or "unknown", outcome="ok")
    return http._json_response(200, payload)


def list_audit(event):
    """Operator action audit trail, newest first (audit.api_recent).

    Same domain function the agent route serves the CLI; rows are projected
    to the audit module's display fields, so no internal key can leak.
    """
    query = event.get("queryStringParameters") or {}
    status, payload = audit_log.api_recent(
        limit=query.get("limit", 50), next_token=query.get("next") or None,
        **audit_log.filters_from_query(query))
    return http._json_response(status, payload)


def export_audit(event, operator):
    """The audit trail as CSV (audit.api_export): the list's filters, one
    bounded export.

    The response carries {filename, count, truncated, csv}; the console turns
    it into a download and the CLI writes the file. The export itself is
    audited, so bulk reads of the trail leave a mark in the trail.
    """
    query = event.get("queryStringParameters") or {}
    status, payload = audit_log.api_export(
        max_rows=query.get("max_rows"), **audit_log.filters_from_query(query))
    if status == 200:
        session._audit_event("audit-log", "audit.export", operator or "unknown",
                             outcome="ok")
    return http._json_response(status, payload)


