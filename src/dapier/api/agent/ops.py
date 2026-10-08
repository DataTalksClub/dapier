"""Operational read and action endpoints: operator overview, runs
and their replay/cancel/export, host agent tasks and workers,
usage and quota, error summary and digest, audit trail, per-
workflow storage, and the trigger inbox."""

import json

from ... import error_digest, host_jobs, host_tasks, host_workers
from ...engine import usage
from .. import errors as errors_api, runs
from .. import storage as storage_api
from ...triggers import inbox
from .. import overview

from .common import _json_response, _no_store, _api_token, _visibility, _write_denied

from .common import _LateBinding

# Shared dependencies resolved through the agent package at call time:
# tests patch agent.<name> and every route module must see the patch.
authenticate = _LateBinding("authenticate")
require_operator = _LateBinding("require_operator")
_is_operator = _LateBinding("_is_operator")
_tables = _LateBinding("_tables")
audit = _LateBinding("audit")
verify_id_token = _LateBinding("verify_id_token")


__all__ = ["agent_tasks_api", "audit_api", "audit_export_api", "errors_digest_api", "errors_summary_api", "host_jobs_api", "inbox_api", "inbox_replay_api", "operator_overview", "quota_api", "runs_api", "runs_cancel_api", "runs_export_api", "runs_replay_api", "runs_replay_failed_api", "runs_resolve_api", "storage_delete_api", "storage_read_api", "storage_write_api", "usage_api", "workers_api"]



def operator_overview(event):
    """Operator-only read view mirroring the console overview (G17
    read-filtered for non-operators like the console route)."""
    subject, error = require_operator(event, "overview")
    if error:
        return error
    return overview.overview(event, visible=_visibility(event, subject))


def runs_api(event, run_id=None):
    """Operator-only run history mirroring the console Runs view.

    Without a run id: the recent-run list, one row per workflow handling of a
    trigger event (G17 read-filtered for non-operators like the console
    route). With one: the run's step-by-step flow (status, input,
    output, duration, error per step).
    """
    subject, error = require_operator(event, "runs")
    if error:
        return error
    if run_id:
        status, payload = runs.api_get(run_id, visible=_visibility(event, subject))
        return _no_store(_json_response(status, payload))
    query = event.get("queryStringParameters") or {}
    status, payload = runs.api_list(
        query.get("limit", 25),
        workflow_id=query.get("workflow_id") or query.get("workflow") or None,
        status=query.get("status") or None,
        since=query.get("since") or None,
        before=query.get("before") or None,
        q=query.get("q") or None,
        next_token=query.get("next") or None,
        visible=_visibility(event, subject),
    )
    return _no_store(_json_response(status, payload))


def agent_tasks_api(event):
    """Operator-only host task list mirroring /api/admin/agent-tasks.

    Same domain function (host_tasks.api_list) as the console route, so the
    CLI and the console see the same rows: what the agent action enqueued,
    what `dapier worker` did with it (status, session, error, timestamps).
    Like the runs read it is not itself audited — require_operator records
    the denials.
    """
    _, error = require_operator(event, "agent-tasks")
    if error:
        return error
    query = event.get("queryStringParameters") or {}
    if query.get("task_id"):
        status, payload = host_tasks.api_get(query["task_id"])
    else:
        status, payload = host_tasks.api_list(
            limit=query.get("limit"), status=query.get("status"))
    return _no_store(_json_response(status, payload))


def workers_api(event):
    """Operator-only host worker list mirroring /api/admin/workers.

    Same domain function (host_workers.api_list) as the console route, so
    `dapier workers list` and the console Workers page see the same rows:
    which `dapier worker` processes checked in, which is active, what each
    is running. Like the agent-tasks read it is not itself audited —
    require_operator records the denials.
    """
    _, error = require_operator(event, "workers")
    if error:
        return error
    return _no_store(_json_response(*host_workers.api_list()))


def host_jobs_api(event, operation):
    """Machine-only host job protocol. An operator login cannot claim jobs."""
    subject, error = authenticate(event)
    if error:
        return error
    token = _api_token(event)
    if not token or token.get("agent") != "host-worker":
        return _json_response(403, {"error": "Host worker token required"})
    try:
        body = json.loads(event.get("body") or "{}")
    except (TypeError, ValueError):
        body = {}
    if not isinstance(body, dict):
        body = {}
    if operation == "claim":
        status, payload = host_jobs.claim(subject, body)
    else:
        if operation == "heartbeat":
            status, payload = host_jobs.heartbeat(body, subject)
        elif operation == "attachment":
            status, payload = host_jobs.attachment(body, subject)
        else:
            status, payload = host_jobs.finish(body, subject)
    return _no_store(_json_response(status, payload))


def _request_body(event):
    try:
        body = json.loads(event.get("body") or "{}")
    except (TypeError, ValueError):
        return {}
    return body if isinstance(body, dict) else {}


def runs_export_api(event):
    """Operator-only run history CSV export (runs.api_export): the list's
    filters, one bounded export served as {filename, count, truncated, csv}.

    Mirrors the audit CSV export: the export itself is audited (runs.export),
    so bulk reads of run history leave a mark in the trail; denials are
    recorded by require_operator.
    """
    subject, error = require_operator(event, "runs.export")
    if error:
        return error
    query = event.get("queryStringParameters") or {}
    status, payload = runs.api_export(
        max_rows=query.get("max_rows"),
        workflow_id=query.get("workflow_id") or query.get("workflow") or None,
        status=query.get("status") or None,
        since=query.get("since") or None,
        before=query.get("before") or None,
        q=query.get("q") or None,
        visible=_visibility(event, subject),
    )
    if status == 200:
        audit.emit("runs", "runs.export", subject, outcome="ok")
    return _no_store(_json_response(status, payload))


def usage_api(event):
    """Operator-only task usage rollup: tasks per workflow per month.

    Carries the quota block alongside the rollup so one read shows both the
    spend and the budget it counts against. The per-workflow rows are G17
    read-filtered for non-operators like the console route; the quota block
    is account-wide and untouched.
    """
    subject, error = require_operator(event, "usage")
    if error:
        return error
    query = event.get("queryStringParameters") or {}
    status, payload = usage.api_usage(query.get("months", 12),
                                      visible=_visibility(event, subject))
    payload["quota"] = usage.quota_status()
    return _no_store(_json_response(status, payload))


def quota_api(event, method):
    """Operator-only monthly task quota: show the budget, or store/clear
    the limit with PUT ({"limit": 1000}, {"limit": "off"} clears it).

    Mirrors the console's /api/admin/quota on the same domain functions in
    engine.usage — the budget the worker's gate enforces on every action
    step. Settings writes land in the audit trail.
    """
    subject, error = require_operator(event, "quota.set" if method == "PUT" else "quota")
    if error:
        return error
    if method == "GET":
        status, payload = usage.api_quota_get()
        return _no_store(_json_response(status, payload))
    try:
        body = json.loads(event.get("body") or "{}")
    except (ValueError, AttributeError, json.JSONDecodeError):
        return _json_response(400, {"error": "Invalid request"})
    if not isinstance(body, dict):
        return _json_response(400, {"error": "Invalid request"})
    status, payload = usage.api_quota_set(body.get("limit"))
    if status == 200:
        audit.emit("usage", "quota.set", subject, outcome="ok")
    return _no_store(_json_response(status, payload))


def errors_summary_api(event):
    """Operator-only failed-run counts by workflow, mirroring the console's.

    Same domain function as /api/admin/errors/summary (api/errors.py), so
    the CLI and the console see the same grouping over the same window.
    """
    subject, error = require_operator(event, "errors")
    if error:
        return error
    query = event.get("queryStringParameters") or {}
    status, payload = errors_api.api_summary(query.get("days", 7),
                                             visible=_visibility(event, subject))
    return _no_store(_json_response(status, payload))


def errors_digest_api(event):
    """Operator-only send-now for the daily error digest.

    Same domain function the scheduled ErrorDigestFunction Lambda runs
    (error_digest.send); the response reports what was sent, or
    ``skipped`` when nothing failed in the window — no noise email.
    """
    subject, error = require_operator(event, "errors.send-digest")
    if error:
        return error
    payload = error_digest.send()
    if payload.get("sent"):
        audit.emit("errors", "errors.send-digest", subject, outcome="ok")
    return _no_store(_json_response(200, payload))


def audit_api(event):
    """Operator-only audit trail: the operator actions audit.record writes
    (connects, grants, token issues, workflow saves), newest first.

    Same domain function as /api/admin/audit (audit.api_recent) — rows are
    projected to the display fields, so nothing beyond what the audit module
    stores can surface. Mirrors the usage/errors reads: operator-gated and
    no-store.
    """
    _, error = require_operator(event, "audit")
    if error:
        return error
    query = event.get("queryStringParameters") or {}
    status, payload = audit.api_recent(
        limit=query.get("limit", 50), next_token=query.get("next") or None,
        **audit.filters_from_query(query))
    return _no_store(_json_response(status, payload))


def audit_export_api(event):
    """Operator-only audit trail CSV export (audit.api_export): the list's
    filters, one bounded export served as {filename, count, truncated, csv}.

    The export itself is audited, so bulk reads of the trail leave a mark in
    the trail; denials are recorded by require_operator.
    """
    subject, error = require_operator(event, "audit.export")
    if error:
        return error
    query = event.get("queryStringParameters") or {}
    status, payload = audit.api_export(
        max_rows=query.get("max_rows"), **audit.filters_from_query(query))
    if status == 200:
        audit.emit("audit-log", "audit.export", subject, outcome="ok")
    return _no_store(_json_response(status, payload))


def storage_read_api(event, workflow_id):
    """Operator-only workflow storage: one key, or the keys under a prefix.

    Same domain layer as the storage_* actions (api/storage.py over
    engine.actions.storage), so the CLI sees exactly what a run sees.
    """
    subject, error = require_operator(event, "storage.read")
    if error:
        return error
    query = event.get("queryStringParameters") or {}
    key = str(query.get("key") or "").strip()
    if key:
        status, payload = storage_api.get(workflow_id, key,
                                          visible=_visibility(event, subject))
    else:
        status, payload = storage_api.find(workflow_id, query.get("prefix"),
                                           query.get("limit"),
                                           visible=_visibility(event, subject))
    return _no_store(_json_response(status, payload))


def storage_write_api(event, workflow_id):
    """Operator-only workflow storage write: ``{key, value, ttl_seconds}``."""
    subject, error = require_operator(event, "storage.write")
    if error:
        return error
    denied = _write_denied(event, subject, workflow_id, "storage.write")
    if denied:
        return denied
    try:
        body = json.loads(event.get("body") or "{}")
    except ValueError:
        return _json_response(400, {"error": "Body must be JSON"})
    status, payload = storage_api.set_value(workflow_id, body)
    return _no_store(_json_response(status, payload))


def storage_delete_api(event, workflow_id):
    """Operator-only workflow storage delete: ``?key=``."""
    subject, error = require_operator(event, "storage.write")
    if error:
        return error
    denied = _write_denied(event, subject, workflow_id, "storage.write")
    if denied:
        return denied
    query = event.get("queryStringParameters") or {}
    status, payload = storage_api.delete(workflow_id, query.get("key"))
    return _no_store(_json_response(status, payload))


def runs_replay_api(event, run_id):
    """Operator-only run replay, mirroring the console's replay button.

    Re-injects the run's original trigger event onto the event queue; the
    worker re-executes it and the rerun lands in run history like a normal
    run. A body ``from_step`` (a top-level step id) targets the replay:
    the recorded outputs before that step seed the rerun, so a long chain
    is retried at the step that failed.
    """
    subject, error = require_operator(event, "runs.replay")
    if error:
        return error
    try:
        body = json.loads(event.get("body") or "{}")
    except (ValueError, AttributeError, json.JSONDecodeError):
        return _json_response(400, {"error": "Invalid request"})
    from_step = str((body or {}).get("from_step") or "").strip()
    status, payload = runs.api_replay(run_id, from_step=from_step or None)
    if status == 202:
        audit.emit(run_id, "runs.replay-from-step" if from_step else "runs.replay",
                   subject, outcome="ok")
    return _no_store(_json_response(status, payload))


def runs_cancel_api(event, run_id):
    """Operator-only cancel of a suspended run, mirroring the console's.

    Flips the run's still-``delayed`` steps to ``cancelled`` (runs.api_cancel)
    so the parked continuation is dropped: when the envelope next surfaces the
    worker finds the pause cancelled and consumes it — the remaining actions
    never fire.
    """
    subject, error = require_operator(event, "runs.cancel")
    if error:
        return error
    status, payload = runs.api_cancel(run_id)
    if status == 200:
        audit.emit(run_id, "runs.cancel", subject, outcome="ok")
    return _no_store(_json_response(status, payload))


def runs_resolve_api(event, run_id):
    """Operator-only mark-a-failure-fixed, mirroring the console's button.

    Stamps ``runs.resolve`` on the run's steps (runs.api_resolve) so the
    failure stops counting as a problem in every view and in
    ``dapier errors``; the run keeps its failed status and error in history.
    The stamp is audited — resolving a failure is an operator's decision
    about work nobody will do, and the trail should say who and when.
    """
    subject, error = require_operator(event, "runs.resolve")
    if error:
        return error
    status, payload = runs.api_resolve(run_id, note=_request_body(event).get("note"),
                                       by=subject)
    if status == 200 and not payload.get("already_resolved"):
        audit.emit(run_id, "runs.resolve", subject, outcome="ok")
    return _no_store(_json_response(status, payload))


def runs_replay_failed_api(event):
    """Operator-only bulk replay: re-inject the workflow's unresolved failures.

    Same path as a single replay, applied to up to MAX_REPLAY_FAILED failed
    runs of the workflow named in the body that are still unresolved — a run
    an operator marked fixed, or one a completed rerun already recovered, is
    not what this is for. Runs without recorded event data are skipped with a
    reason.
    """
    subject, error = require_operator(event, "runs.replay-failed")
    if error:
        return error
    try:
        body = json.loads(event.get("body") or "{}")
    except (ValueError, AttributeError, json.JSONDecodeError):
        return _json_response(400, {"error": "Invalid request"})
    workflow_id = str((body or {}).get("workflow_id") or "").strip()
    status, payload = runs.api_replay_failed(workflow_id)
    if status == 202:
        audit.emit(workflow_id, "runs.replay-failed", subject, outcome="ok")
    return _no_store(_json_response(status, payload))


def inbox_api(event, inbox_id=None):
    """Operator-only trigger inbox: every inbound event, matched or not.

    Without an id: recent events, filterable by connector — the row the run
    history never shows for events no workflow claimed (G17 read-filtered
    for non-operators like the console route). With one: the stored
    envelope (data, matched workflows, status).
    """
    subject, error = require_operator(event, "triggers.inbox")
    if error:
        return error
    if inbox_id:
        status, payload = inbox.api_get(inbox_id,
                                        visible=_visibility(event, subject))
        return _no_store(_json_response(status, payload))
    query = event.get("queryStringParameters") or {}
    status, payload = inbox.api_list(
        query.get("connector"), query.get("limit", 25),
        next_token=query.get("next") or None,
        visible=_visibility(event, subject),
    )
    return _no_store(_json_response(status, payload))


def inbox_replay_api(event, inbox_id):
    """Operator-only inbox replay: send a recorded event through the engine.

    The event goes back on the queue with a fresh id, so workflows are
    matched afresh and the rerun lands in run history like a normal run —
    the "test this trigger" button for events that arrived before their
    workflow existed.
    """
    subject, error = require_operator(event, "triggers.inbox-replay")
    if error:
        return error
    status, payload = inbox.api_replay(inbox_id)
    if status == 202:
        audit.emit(inbox_id, "triggers.inbox-replay", subject, outcome="ok")
    return _no_store(_json_response(status, payload))
