"""Operator run endpoints: the history list, the CSV export,
replays, cancel, resolve, and the replay-failed bulk op."""
from ... import http
import json
from .. import runs
from ...auth import session


def list_runs(event, visible=None):
    """Recent runs, one row per workflow handling of a trigger event.

    ``visible`` (G17 auth.visibility, built by the dispatcher from the
    session) read-filters the list for non-operators."""
    query = event.get("queryStringParameters") or {}
    status, payload = runs.api_list(
        query.get("limit", 25),
        workflow_id=query.get("workflow_id") or query.get("workflow") or None,
        status=query.get("status") or None,
        since=query.get("since") or None,
        before=query.get("before") or None,
        q=query.get("q") or None,
        next_token=query.get("next") or None,
        visible=visible,
    )
    return http._json_response(status, payload)


def export_runs(event, operator, visible=None):
    """Run history as CSV (runs.api_export): the list's filters, one
    bounded export.

    The response carries {filename, count, truncated, csv}; the console
    turns it into a download and the CLI writes the file. The export is
    audited like the audit CSV export: bulk reads leave a mark in the trail.
    ``visible`` applies the G17 read filter exactly as the list does, so
    the CSV cannot see past it.
    """
    query = event.get("queryStringParameters") or {}
    status, payload = runs.api_export(
        max_rows=query.get("max_rows"),
        workflow_id=query.get("workflow_id") or query.get("workflow") or None,
        status=query.get("status") or None,
        since=query.get("since") or None,
        before=query.get("before") or None,
        q=query.get("q") or None,
        visible=visible,
    )
    if status == 200:
        session._audit_event("runs", "runs.export", operator or "unknown",
                             outcome="ok")
    return http._json_response(status, payload)




def get_run(run_id, visible=None):
    """One run's step-by-step flow: status, input, output, duration, error.

    ``visible`` scopes the single read exactly as the runs list does: a
    hidden run answers like a missing one."""
    status, payload = runs.api_get(run_id, visible=visible)
    return http._json_response(status, payload)


def replay_run(run_id, operator, event=None):
    """Re-execute a run: its original trigger event goes back on the queue.

    A body ``from_step`` (a top-level step id) replays from that step
    instead: the recorded outputs before it seed the rerun, so a long
    chain is retried at the step that failed. The audit trail names the
    targeted replay distinctly.
    """
    try:
        body = json.loads(event.get("body") or "{}") if event else {}
    except (ValueError, json.JSONDecodeError):
        return http._json_response(400, {"error": "Invalid request"})
    from_step = str((body or {}).get("from_step") or "").strip()
    status, payload = runs.api_replay(run_id, from_step=from_step or None)
    if status == 202:
        session._audit_event(run_id, "runs.replay-from-step" if from_step
                             else "runs.replay", operator or "unknown", outcome="ok")
    return http._json_response(status, payload)


def cancel_run(run_id, operator):
    """Cancel a suspended run: its parked steps close out cancelled and the
    parked continuation is dropped (the worker consumes the envelope)."""
    status, payload = runs.api_cancel(run_id)
    if status == 200:
        session._audit_event(run_id, "runs.cancel", operator or "unknown", outcome="ok")
    return http._json_response(status, payload)


def replay_failed_runs(event, operator):
    """Re-execute the latest unresolved failed runs of the workflow named in
    the body — the ones a fix has not already settled."""
    try:
        body = json.loads(event.get("body") or "{}")
    except (ValueError, json.JSONDecodeError):
        return http._json_response(400, {"error": "Invalid request"})
    workflow_id = str((body or {}).get("workflow_id") or "").strip()
    status, payload = runs.api_replay_failed(workflow_id)
    if status == 202:
        session._audit_event(workflow_id, "runs.replay-failed",
                             operator or "unknown", outcome="ok")
    return http._json_response(status, payload)


def resolve_run(run_id, operator, event=None):
    """Mark a failed run fixed: it drops out of the failure views.

    What the console's "Mark fixed" button and ``dapier runs resolve`` both
    call. The stamp lands on the run's steps (runs.api_resolve), the run
    keeps its failed status in history, and the audit trail records the
    operator's call — resolving a failure is a decision, not a computation.
    """
    try:
        body = json.loads(event.get("body") or "{}") if event else {}
    except (ValueError, json.JSONDecodeError):
        return http._json_response(400, {"error": "Invalid request"})
    status, payload = runs.api_resolve(run_id, note=(body or {}).get("note"),
                                       by=operator or "unknown")
    if status == 200 and not payload.get("already_resolved"):
        session._audit_event(run_id, "runs.resolve", operator or "unknown",
                             outcome="ok")
    return http._json_response(status, payload)


