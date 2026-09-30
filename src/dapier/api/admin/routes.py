"""Operator console endpoints: thin JSON wrappers over the domain modules."""
import json
import os

import boto3

from ... import audit as audit_log
from ... import error_digest
from ... import host_tasks
from ... import host_workers
from ... import http
from ...auth import api_tokens, authz, roles, session
from ... import copilot
from ...connections import credentials, importing, zoom
from ...connectors import trigger_discovery
from ...connections import records as connection_model
from ...connections.providers import oauth_clients
from ...triggers import email_from, email_triggers, hook_triggers, inbox, poll_triggers, schedule_triggers
from ...engine import usage as usage_rollup
from .. import designer_store, discovery as discovery_api, errors as errors_api, overview, runs
from .. import storage as storage_api


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


def export_all_designer_workflows(event, operator, visible=None):
    """Every workflow's canonical YAML as one zip (designer_store.api_export_all).

    Same domain function the agent route serves the CLI; the response carries
    {filename, count, skipped, b64} — the console decodes the base64 zip into
    a download. ``visible`` leaves out the workflows the caller may not see.
    The bulk export is audited like the audit CSV export: bulk
    reads leave a mark in the trail.
    """
    status, payload = designer_store.api_export_all(visible=visible)
    if status == 200:
        session._audit_event("workflows", "workflow.export-all", operator or "unknown",
                             outcome="ok")
    return http._json_response(status, payload)


def export_designer_workflows(event, operator, visible=None):
    """Every workflow's canonical YAML as one zip (designer_store.api_export),
    optionally narrowed with ``?tag=`` / ``?folder=`` like the designer list.

    Same domain function the agent route serves `workflows export --all`; the
    response carries {filename, count, skipped, b64} and an attachment
    content-disposition carrying the dated filename — the console decodes the
    base64 zip into that download. ``visible`` leaves out the workflows the
    caller may not see. The bulk export is audited like the audit CSV export:
    bulk reads leave a mark in the trail.
    """
    query = event.get("queryStringParameters") or {}
    status, payload = designer_store.api_export(tag=query.get("tag"),
                                                folder=query.get("folder"),
                                                visible=visible)
    if status == 200:
        session._audit_event("workflows", "workflow.export", operator or "unknown",
                             outcome="ok")
        return http._json_response(status, payload, headers={
            "content-disposition": f'attachment; filename="{payload["filename"]}"'})
    return http._json_response(status, payload)


def storage_read(event, workflow_id, visible=None):
    """Workflow storage: one key (``key=``) or the keys under ``prefix=``.

    ``visible`` scopes the reads exactly as the writes gate: a hidden
    partition answers like an empty one."""
    query = event.get("queryStringParameters") or {}
    key = str(query.get("key") or "").strip()
    if key:
        status, payload = storage_api.get(workflow_id, key, visible=visible)
    else:
        status, payload = storage_api.find(workflow_id, query.get("prefix"),
                                           query.get("limit"), visible=visible)
    return http._json_response(status, payload)


def storage_write(event, workflow_id, visible=None, operator=None):
    """Store one workflow storage value: ``{key, value, ttl_seconds}``."""
    denied = _write_denied(visible, workflow_id, "storage.write", operator)
    if denied:
        return denied
    try:
        body = json.loads(event.get("body") or "{}")
    except ValueError:
        return http._json_response(400, {"error": "Body must be JSON"})
    status, payload = storage_api.set_value(workflow_id, body)
    return http._json_response(status, payload)


def storage_delete(event, workflow_id, visible=None, operator=None):
    """Remove one workflow storage value: ``?key=``."""
    denied = _write_denied(visible, workflow_id, "storage.write", operator)
    if denied:
        return denied
    query = event.get("queryStringParameters") or {}
    status, payload = storage_api.delete(workflow_id, query.get("key"))
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
    """Re-execute the latest failed runs of the workflow named in the body."""
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


def list_inbox(event, visible=None):
    """Trigger inbox: every inbound event, matched or not.

    ``visible`` (G17 auth.visibility) read-filters the list for
    non-operators: events that matched at least one workflow they own;
    unmatched events stay visible to everyone."""
    query = event.get("queryStringParameters") or {}
    status, payload = inbox.api_list(
        query.get("connector"), query.get("limit", 25),
        next_token=query.get("next") or None,
        visible=visible,
    )
    return http._json_response(status, payload)


def get_inbox_event(inbox_id, visible=None):
    """One inbox event: the stored envelope and the workflows that matched.

    ``visible`` scopes the single read exactly as the list does: a hidden
    event answers like a missing one."""
    status, payload = inbox.api_get(inbox_id, visible=visible)
    return http._json_response(status, payload)


def replay_inbox_event(inbox_id, operator):
    """Re-send an inbox event through the engine (fresh id, same data)."""
    status, payload = inbox.api_replay(inbox_id)
    if status == 202:
        session._audit_event(inbox_id, "triggers.inbox-replay",
                             operator or "unknown", outcome="ok")
    return http._json_response(status, payload)


def oauth_clients_view():
    return http._json_response(200, {
        "clients": [overview._oauth_client_status(provider) for provider in oauth_clients.CANONICAL_PROVIDERS],
    })

def save_oauth_client(provider, event):
    """Store the shared OAuth client for a provider in the config DB.

    Runtime-reconfigurable: no redeploy needed. The secret is write-only —
    the response reports presence, not the value.
    """
    try:
        body = http._request_json(event)
    except (ValueError, json.JSONDecodeError):
        return http._json_response(400, {"error": "Invalid request"})
    status, payload = oauth_clients.api_save_client(
        provider, body.get("client_id"), body.get("client_secret"))
    if status == 200:
        session._audit_event(f"oauth-client#{payload['provider']}", audit_log.CONFIG,
                     session._session_subject(event) or "unknown", outcome="ok")
    return http._json_response(status, payload)

def save_credential(provider, event):
    try:
        body = http._request_json(event)
    except (ValueError, json.JSONDecodeError):
        return http._json_response(400, {"error": "Invalid request"})
    status, payload = credentials.api_save_credential(provider, body)
    return http._json_response(status, payload)

def save_connection(event):
    try:
        body = http._request_json(event)
    except (ValueError, json.JSONDecodeError):
        return http._json_response(400, {"error": "Invalid request"})
    try:
        fields = connection_model.validate_new_connection(body)
    except connection_model.ConnectionError as exc:
        return http._json_response(400, {"error": str(exc)})

    connections_table = boto3.resource("dynamodb").Table(os.environ["CONNECTIONS_TABLE"])
    previous = connection_model.get_connection(connections_table, fields["connection_id"])
    operator = session._session_subject(event)

    if fields["provider"] == "zoom":
        status, payload = zoom.save(body, operator_subject=operator, connections_table=connections_table)
        session._audit_event(fields["connection_id"], audit_log.CONNECT, operator or "unknown",
                             outcome="ok" if status == 200 else "error")
        return http._json_response(status, payload)
    if fields["provider"] in connection_model.TOKEN_PROVIDERS:
        status, payload = importing.save_token_connection(
            body, operator_subject=operator, connections_table=connections_table,
            audit_event=session._audit_event, action=audit_log.CONNECT,
            reuse_stored_token=True)
        return http._json_response(status, payload)

    try:
        item = connection_model.build_item(
            fields, owner_subject=operator, previous=previous,
        )
    except connection_model.BindingError as exc:
        session._audit_event(fields["connection_id"], audit_log.CONNECT, operator or "unknown",
                     outcome="error", error=str(exc))
        return http._json_response(409, {"error": str(exc)})

    connection_model.put_connection(connections_table, item)
    session._audit_event(item["connection_id"], audit_log.CONNECT, operator or "unknown", outcome="ok")
    return http._json_response(200, item)

def list_connections(event):
    """The paged connections list (records.api_list_connections): every
    connection, not just the overview snapshot's first scan page, with
    ``limit``/``next`` paging behind the console's Load more and
    `dapier connections list --all`. Rows carry the same public metadata
    plus token health the overview's connections block renders, and the
    ``used_in`` map saying which workflows and hook triggers reference
    each one."""
    query = event.get("queryStringParameters") or {}
    connections_table = boto3.resource("dynamodb").Table(os.environ["CONNECTIONS_TABLE"])
    status, payload = connection_model.api_list_connections(
        connections_table, limit=query.get("limit"),
        next_token=query.get("next") or None,
    )
    if status == 200:
        payload["connections"] = overview._connection_views(payload["connections"])
        from ...triggers import connection_usage
        connection_usage.attach(payload["connections"])
    return http._json_response(status, payload)

def list_grants(event):
    query = event.get("queryStringParameters") or {}
    status, payload = authz.api_list_grants(
        authz.grants_table(), connection_id=query.get("connection_id") or None,
        limit=query.get("limit"), next_token=query.get("next") or None,
    )
    return http._json_response(status, payload)

def save_grant(event, operator):
    try:
        body = http._request_json(event)
    except (ValueError, json.JSONDecodeError):
        return http._json_response(400, {"error": "Invalid request"})
    status, payload = authz.api_save_grant(
        authz.grants_table(), body, operator=operator,
        connections_table=boto3.resource("dynamodb").Table(os.environ["CONNECTIONS_TABLE"]),
    )
    if status == 200:
        session._audit_event(payload["connection_id"], audit_log.GRANT, operator,
                     agent=payload["agent"], outcome="ok")
    return http._json_response(status, payload)

def delete_grant(event, operator):
    query = event.get("queryStringParameters") or {}
    status, payload = authz.api_delete_grant(
        authz.grants_table(), query.get("connection_id"), query.get("grantee"),
    )
    if status == 200:
        session._audit_event(str(query.get("connection_id", "")).strip().lower(),
                     audit_log.GRANT, operator, outcome="revoked")
    return http._json_response(status, payload)

def list_email_triggers(event):
    status, payload = email_triggers.api_list()
    return http._json_response(status, payload)

def save_email_trigger(event, operator):
    try:
        body = http._request_json(event)
        status, payload = email_triggers.api_save(body, operator)
    except (ValueError, json.JSONDecodeError) as exc:
        return http._json_response(400, {"error": str(exc) or "Invalid request"})
    session._audit_event(payload.get("name", "unknown"), "email-trigger.save", operator,
                 outcome="created" if payload.get("created") else "updated")
    return http._json_response(status, payload)

def delete_email_trigger(event, operator):
    query = event.get("queryStringParameters") or {}
    try:
        status, payload = email_triggers.api_delete(query.get("name", ""), operator)
    except email_triggers.TriggerError as exc:
        return http._json_response(404, {"error": str(exc)})
    session._audit_event(payload.get("name", "unknown"), "email-trigger.delete", operator, outcome="deleted")
    return http._json_response(status, payload)


def email_from_list(event):
    try:
        status, payload = email_from.api_list()
    except email_from.FromError as exc:
        return http._json_response(400, {"error": str(exc)})
    return http._json_response(status, payload)


def email_from_add(event, operator):
    try:
        body = http._request_json(event)
        status, payload = email_from.api_add((body or {}).get("address"))
    except (email_from.FromError, ValueError, json.JSONDecodeError) as exc:
        return http._json_response(400, {"error": str(exc) or "Invalid request"})
    session._audit_event("email-from", "email-from", operator, outcome="added")
    return http._json_response(status, payload)


def email_from_remove(event, operator):
    query = event.get("queryStringParameters") or {}
    try:
        status, payload = email_from.api_remove(query.get("address", ""))
    except email_from.FromError as exc:
        return http._json_response(400, {"error": str(exc)})
    session._audit_event("email-from", "email-from", operator, outcome="removed")
    return http._json_response(status, payload)


def agent_tasks_list(event):
    """Host tasks the agent action enqueued (status, error, timestamps).

    Read-only: the session/role gate in the dispatcher covers it, and like
    the other list reads the read itself is not audited.
    """
    query = event.get("queryStringParameters") or {}
    if query.get("task_id"):
        status, payload = host_tasks.api_get(query["task_id"])
    else:
        status, payload = host_tasks.api_list(
            limit=query.get("limit"), status=query.get("status"))
    return http._json_response(status, payload)


def workers_list(event):
    """Host workers (`dapier worker` processes) and their presence.

    Read-only: the session/role gate in the dispatcher covers it, and like
    the other list reads the read itself is not audited. The same rows feed
    `dapier workers list` through /api/agent/workers.
    """
    return http._json_response(*host_workers.api_list())


def _write_denied(visible, workflow_id, action, operator):
    """G17 Phase 3: the owner-or-operator write gate for the workflow-scoped
    console routes — a 403 response when ``visible`` (the dispatcher's
    session scope) targets a workflow it does not own, else ``None``.
    ``visible`` is None for unrestricted callers (operator sessions resolve
    to an unrestricted scope; legacy direct calls pass nothing);
    visibility.ensure_can_write holds the rule, and denials are audited
    like the role gates'."""
    if visible is None:
        return None
    denied = visible.can_write(workflow_id)
    if denied is None:
        return None
    session._audit_event(str(workflow_id or "unknown"), action,
                         operator or "unknown", outcome="denied-not-owner")
    return http._json_response(*denied)


def _save_denied(visible, body, operator):
    """The save gate over every id a save body touches (the definition's id
    plus a renameFrom target)."""
    for workflow_id in designer_store.save_gate_ids(body):
        denied = _write_denied(visible, workflow_id, "workflow.save", operator)
        if denied is not None:
            return denied
    return None


def designer_list(event, visible=None):
    """The designer list, G17 read-filtered for non-operators (``visible``
    is built by the dispatcher from the session; None = unrestricted)."""
    query = event.get("queryStringParameters") or {}
    status, payload = designer_store.api_list(query.get("q") or None,
                                              tag=query.get("tag") or None,
                                              folder=query.get("folder") or None,
                                              visible=visible)
    return http._json_response(status, payload)

def designer_get(source, visible=None):
    status, payload = designer_store.api_get(source, visible=visible)
    return http._json_response(status, payload)

def save_designer_workflow(event, operator, visible=None):
    try:
        body = http._request_json(event)
        denied = _save_denied(visible, body, operator)
        if denied:
            return denied
        status, payload = designer_store.api_save(body, operator=operator)
    except (ValueError, json.JSONDecodeError) as exc:
        return http._json_response(400, {"error": str(exc) or "Invalid request"})
    session._audit_event(str(payload.get("file", "unknown")), "workflow.save", operator,
                 outcome="ok" if status == 200 else "error", error=payload.get("error"))
    return http._json_response(status, payload)

def copilot_draft(event, operator):
    """Console mirror of the agent copilot: a DRAFT workflow for a prompt.

    Delegates to the same copilot.draft_workflow handler as the CLI-facing
    /api/agent/copilot/draft; never saves or publishes.
    """
    try:
        body = http._request_json(event)
    except (ValueError, json.JSONDecodeError):
        return http._json_response(400, {"error": "Invalid request"})
    if not isinstance(body, dict):
        return http._json_response(400, {"error": "Invalid request"})
    status, payload = copilot.draft_workflow(body.get("prompt"))
    session._audit_event("copilot", "workflow.draft", operator,
                 outcome="ok" if status == 200 else "error", error=payload.get("error"))
    return http._json_response(status, payload)


def toggle_designer_workflow(event, operator, source, visible=None):
    denied = _write_denied(visible, str(source).removesuffix(".yaml"),
                           "workflow.toggle", operator)
    if denied:
        return denied
    try:
        body = http._request_json(event)
        status, payload = designer_store.api_toggle(source, body, operator=operator)
    except (ValueError, json.JSONDecodeError) as exc:
        return http._json_response(400, {"error": str(exc) or "Invalid request"})
    session._audit_event(str(source), "workflow.toggle", operator,
                 outcome="ok" if status == 200 else "error", error=payload.get("error"))
    return http._json_response(status, payload)

def duplicate_designer_workflow(event, operator, source):
    """Console mirror of the CLI duplicate: copy a workflow under a new id."""
    try:
        body = http._request_json(event)
        status, payload = designer_store.api_duplicate(source, body, operator=operator)
    except (ValueError, json.JSONDecodeError) as exc:
        return http._json_response(400, {"error": str(exc) or "Invalid request"})
    session._audit_event(str(payload.get("file", source or "unknown")), "workflow.duplicate", operator,
                 outcome="ok" if status == 200 else "error", error=payload.get("error"))
    return http._json_response(status, payload)

def delete_designer_workflow(event, operator, source, visible=None):
    """Console mirror of the CLI delete: unpublish the live item (version
    records survive — history, not live state), then one atomic git
    tree-delete commit. Refused 409 while runs are parked on a delay, so a
    resume cannot dangle."""
    denied = _write_denied(visible, str(source).removesuffix(".yaml"),
                           "workflow.delete", operator)
    if denied:
        return denied
    status, payload = designer_store.api_delete(source, operator=operator)
    session._audit_event(str(payload.get("file", source or "unknown")), "workflow.delete", operator,
                 outcome="ok" if status == 200 else "error", error=payload.get("error"))
    return http._json_response(status, payload)

def tags_designer_workflow(event, operator, source, visible=None):
    """Console mirror of the CLI tags editor: replace a workflow's tag set
    (Zapier-style organization). Publishes cause "tags" and commits the
    updated YAML best-effort, like the toggle."""
    denied = _write_denied(visible, str(source).removesuffix(".yaml"),
                           "workflow.tags", operator)
    if denied:
        return denied
    try:
        body = http._request_json(event)
        status, payload = designer_store.api_tags(source, body, operator=operator)
    except (ValueError, json.JSONDecodeError) as exc:
        return http._json_response(400, {"error": str(exc) or "Invalid request"})
    session._audit_event(str(payload.get("file", source or "unknown")), "workflow.tags", operator,
                 outcome="ok" if status == 200 else "error", error=payload.get("error"))
    return http._json_response(status, payload)

def folder_designer_workflow(event, operator, source, visible=None):
    """Console mirror of the CLI folder editor: put a workflow in a
    Zapier-style folder (flat — at most one per workflow, an empty string
    clears it). Publishes cause "folder" and commits the updated YAML
    best-effort, like the toggle."""
    denied = _write_denied(visible, str(source).removesuffix(".yaml"),
                           "workflow.folder", operator)
    if denied:
        return denied
    try:
        body = http._request_json(event)
        status, payload = designer_store.api_folder(source, body, operator=operator)
    except (ValueError, json.JSONDecodeError) as exc:
        return http._json_response(400, {"error": str(exc) or "Invalid request"})
    session._audit_event(str(payload.get("file", source or "unknown")), "workflow.folder", operator,
                 outcome="ok" if status == 200 else "error", error=payload.get("error"))
    return http._json_response(status, payload)

def bulk_designer_workflow(event, operator, visible=None):
    """Console bulk enable/disable: one call over the selection bar's ids.

    Each workflow toggles through the same api_toggle semantics and answers
    per id; one audit row covers the batch, with the id list as the subject.
    The G17 write gate rides along per id (api_bulk's ``visible``)."""
    try:
        body = http._request_json(event)
        status, payload = designer_store.api_bulk(body, operator=operator,
                                                  visible=visible)
    except (ValueError, json.JSONDecodeError) as exc:
        return http._json_response(400, {"error": str(exc) or "Invalid request"})
    ids = [str(item) for item in (body or {}).get("ids") or []] if isinstance(body, dict) else []
    subject = ", ".join(ids)
    if len(subject) > 400:
        subject = subject[:400] + f" … (+{len(ids)} total)"
    session._audit_event(subject or "bulk", "workflow.bulk-toggle", operator,
                 outcome="ok" if status == 200 else "error", error=payload.get("error"))
    return http._json_response(status, payload)

def test_designer_workflow(event, operator, source=None, visible=None):
    """Dry-run (or, on execute, really run) one workflow on a sample event."""
    if source:
        # Keyed by a saved workflow: the write gate applies. An inline
        # workflow (the designer's unsaved draft) is nobody's stored row.
        denied = _write_denied(visible, str(source).removesuffix(".yaml"),
                               "workflow.test", operator)
        if denied:
            return denied
    try:
        body = http._request_json(event)
        status, payload = designer_store.api_test_run(source, body, operator=operator)
    except (ValueError, json.JSONDecodeError) as exc:
        return http._json_response(400, {"error": str(exc) or "Invalid request"})
    session._audit_event(str(payload.get("file", source or "unknown")), "workflow.test", operator,
                 outcome="ok" if status == 200 else "error", error=payload.get("error"))
    return http._json_response(status, payload)


def test_designer_step(event, operator, source=None, visible=None):
    """Console mirror of the per-step test (same domain module as the CLI).

    Zapier's "Test step": run one action against the sample event — for real
    with execute: true, side effects limited to that step. The workflow comes
    inline (the designer's unsaved draft) or from a saved file."""
    if source:
        # Keyed by a saved workflow: the write gate applies. An inline
        # workflow (the designer's unsaved draft) is nobody's stored row.
        denied = _write_denied(visible, str(source).removesuffix(".yaml"),
                               "workflow.test", operator)
        if denied:
            return denied
    try:
        body = http._request_json(event)
        status, payload = designer_store.api_test_step(source, body, operator=operator)
    except (ValueError, json.JSONDecodeError) as exc:
        return http._json_response(400, {"error": str(exc) or "Invalid request"})
    session._audit_event(str(payload.get("file", source or "unknown")), "workflow.test-step",
                 operator,
                 outcome="ok" if status == 200 else "error", error=payload.get("error"))
    return http._json_response(status, payload)


def versions_designer_workflow(source, visible=None):
    """Console mirror of the CLI versions list: one workflow's history."""
    status, payload = designer_store.api_versions(source, visible=visible)
    return http._json_response(status, payload)


def diff_designer_workflow(event, source, visible=None):
    """Console mirror of the CLI diff: the unified diff between two
    published versions, for the Versions dialog's Diff button."""
    query = event.get("queryStringParameters") or {}
    status, payload = designer_store.api_diff(source, query.get("from"),
                                              query.get("to"), visible=visible)
    return http._json_response(status, payload)


def rollback_designer_workflow(event, operator, source, visible=None):
    """Console mirror of the CLI rollback: republish an old version."""
    denied = _write_denied(visible, str(source).removesuffix(".yaml"),
                           "workflow.rollback", operator)
    if denied:
        return denied
    try:
        body = http._request_json(event)
        status, payload = designer_store.api_rollback(source, body, operator=operator)
    except (ValueError, json.JSONDecodeError) as exc:
        return http._json_response(400, {"error": str(exc) or "Invalid request"})
    session._audit_event(str(payload.get("file", source or "unknown")), "workflow.rollback", operator,
                 outcome="ok" if status == 200 else "error", error=payload.get("error"))
    return http._json_response(status, payload)

# ---- Draft vs live (G15): a save drafts; publish/discard promote or throw ----

def publish_designer_workflow(event, operator, source, visible=None):
    """Console mirror of `workflows publish`: promote the workflow's draft
    through the ordinary publish path (git, revision, YouTube reconcile).
    409 stale when the live definition moved past the draft's base."""
    denied = _write_denied(visible, str(source).removesuffix(".yaml"),
                           "workflow.publish", operator)
    if denied:
        return denied
    status, payload = designer_store.api_publish(source, operator=operator)
    session._audit_event(str(payload.get("file", source or "unknown")), "workflow.publish", operator,
                 outcome="ok" if status == 200 else "error", error=payload.get("error"))
    return http._json_response(status, payload)

def discard_designer_draft(event, operator, source, visible=None):
    """Console mirror of `workflows discard`: throw the draft away; the live
    definition is untouched."""
    denied = _write_denied(visible, str(source).removesuffix(".yaml"),
                           "workflow.discard", operator)
    if denied:
        return denied
    status, payload = designer_store.api_discard(source, operator=operator)
    session._audit_event(str(source), "workflow.discard", operator,
                 outcome="ok" if status == 200 else "error", error=payload.get("error"))
    return http._json_response(status, payload)

def draft_designer_workflow(source, visible=None):
    """Console mirror of the draft read: one workflow's drafted definition."""
    status, payload = designer_store.api_draft(source, visible=visible)
    return http._json_response(status, payload)

def draft_diff_designer_workflow(source, visible=None):
    """Console mirror of `workflows draft-diff`: draft vs live, api_diff shape."""
    status, payload = designer_store.api_draft_diff(source, visible=visible)
    return http._json_response(status, payload)

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

def list_api_tokens(event):
    status, payload = api_tokens.api_list()
    return http._json_response(status, payload)

def create_api_token(event, operator):
    try:
        body = http._request_json(event)
        status, payload = api_tokens.api_create(body, operator)
    except (ValueError, json.JSONDecodeError) as exc:
        return http._json_response(400, {"error": str(exc) or "Invalid request"})
    if status == 200:
        session._audit_event(f"api-token#{payload['token_id']}", audit_log.API_TOKEN,
                     operator, agent=payload["agent"], outcome="created")
    return http._json_response(status, payload)

def revoke_api_token(event, operator):
    query = event.get("queryStringParameters") or {}
    status, payload = api_tokens.api_revoke(query.get("token_id"))
    if status == 200:
        session._audit_event(f"api-token#{payload.get('token_id', 'unknown')}",
                     audit_log.API_TOKEN, operator, outcome="revoked")
    return http._json_response(status, payload)

def delete_api_token(event, operator):
    """DELETE /api/admin/tokens: revoke by default; purge=1 permanently removes
    an already-revoked token and deletes its grants (console mirror of the
    CLI's `dapier tokens delete`)."""
    query = event.get("queryStringParameters") or {}
    if query.get("purge") not in ("1", "true", "yes"):
        return revoke_api_token(event, operator)
    status, payload = api_tokens.api_delete(
        query.get("token_id"), grants_table_ref=authz.grants_table())
    if status == 200:
        session._audit_event(f"api-token#{payload['token_id']}",
                     audit_log.API_TOKEN, operator, outcome="deleted")
    return http._json_response(status, payload)

def _connection(connection_id):
    return connection_model.get_connection(
        boto3.resource("dynamodb").Table(os.environ["CONNECTIONS_TABLE"]),
        connection_id,
    )

def import_connection(event, operator):
    """One-time operator import of an existing provider credential (cookie path)."""
    try:
        body = http._request_json(event)
    except (ValueError, json.JSONDecodeError):
        return http._json_response(400, {"error": "Invalid request"})
    connections_table = boto3.resource("dynamodb").Table(os.environ["CONNECTIONS_TABLE"])
    status, payload = importing.import_core(body, operator_subject=operator, connections_table=connections_table)
    return http._json_response(status, payload)

def revoke_connection_tokens(connection_id, operator):
    from ...connections import tokens as token_lifecycle

    connection = _connection(connection_id)
    if not connection:
        return http._json_response(404, {"error": "Connection not found"})
    updated = token_lifecycle.revoke_connection(connection)
    boto3.resource("dynamodb").Table(os.environ["CONNECTIONS_TABLE"]).put_item(Item=updated)
    session._audit_event(connection_id, audit_log.REVOKE, operator, outcome="ok")
    return http._json_response(200, {"connection_id": connection_id, "status": updated["status"]})

def delete_connection(event, connection_id, operator):
    """DELETE /api/admin/connections/{id}: remove the connection, its stored
    credential, and its grants outright — the console mirror of the CLI's
    `dapier connections delete`. A 409 names the workflows and hook triggers
    still referencing it; ``?force=1`` accepts breaking those."""
    query = event.get("queryStringParameters") or {}
    status, payload = connection_model.api_delete_connection(
        boto3.resource("dynamodb").Table(os.environ["CONNECTIONS_TABLE"]),
        connection_id,
        grants_table_ref=authz.grants_table(),
        force=query.get("force") in ("1", "true", "yes"),
    )
    if status == 200:
        session._audit_event(connection_id, "connections.delete", operator, outcome="ok")
    return http._json_response(status, payload)

def issue_connection_token(connection_id, operator):
    """Console mirror of the CLI's fresh provider access token (same domain call).

    `dapier token exec|write` mint short-lived provider tokens through
    /api/agent/token; this gives the Connections view the same outcome
    without the CLI. The value is only ever returned to an operator session
    and never persisted.
    """
    from ...connections import tokens as token_lifecycle
    from ...connections.records import BindingError
    from ...connections.tokens import TokenError

    connection = _connection(connection_id)
    if not connection:
        return http._json_response(404, {"error": "Connection not found"})
    try:
        access_token, info = token_lifecycle.get_access_token(connection)
    except BindingError as exc:
        session._audit_event(connection_id, "connections.token", operator or "unknown",
                     outcome="error", error=str(exc))
        return http._json_response(409, {"error": str(exc)})
    except TokenError:
        session._audit_event(connection_id, "connections.token", operator or "unknown",
                     outcome="error", error="provider-token-unavailable")
        return http._json_response(502, {"error": "Provider token is unavailable"})
    session._audit_event(connection_id, "connections.token", operator or "unknown", outcome="ok")
    response = http._json_response(200, {
        "connection_id": connection_id,
        "provider": connection["provider"],
        "access_token": access_token,
        "expires_at": info["expires_at"],
        "scope": info["scope"],
        "provider_account_id": info["provider_account_id"],
        "account_title": info.get("account_title"),
        "refreshed": info["refreshed"],
    })
    response["headers"]["cache-control"] = "no-store"
    return response

def discover_connection(connection_id, event, resource=None):
    """Console mirror of the CLI discovery endpoints (same domain module).

    Without a resource: the provider's discoverable-resource catalog; with
    one: that resource's live items, fetched with the connection's token.
    The domain layer is api.discovery, shared verbatim with the agent API;
    resource metadata comes from the connector registry. Unknown connections
    — including the aws/s3 pseudo-connections — are the domain's 404.
    """
    if resource is None:
        status, payload = discovery_api.resources(connection_id)
        return http._json_response(status, payload)
    query = event.get("queryStringParameters") or {}
    status, payload = discovery_api.discover(connection_id, resource, query)
    return http._json_response(status, payload)

def test_connection(connection_id, event, operator):
    """Console mirror of the connection health test (same domain module)."""
    status, payload = discovery_api.test_connection(connection_id)
    session._audit_event(connection_id, "connections.test", operator or "unknown",
                 outcome="ok" if payload.get("ok") else "error",
                 error=None if payload.get("ok") else str(payload.get("detail")))
    return http._json_response(status, payload)


def discover_samples(event, operator):
    """Console mirror of the CLI's trigger sample pull (same domain dispatch).

    Zapier's 'pull in sample data': a realistic event envelope for one
    trigger connector — live where the connector can fetch, else the newest
    recorded run, else a documented example. The designer's test panel
    and `dapier triggers sample` land here via their own surfaces.
    """
    try:
        body = http._request_json(event)
    except (ValueError, json.JSONDecodeError) as exc:
        return http._json_response(400, {"error": str(exc) or "Invalid request"})
    status, payload = trigger_discovery.api_discover(body)
    session._audit_event(
        str((body or {}).get("connector") or payload.get("connector") or "unknown"),
        "triggers.sample", operator or "unknown",
        outcome="ok" if status == 200 else "error", error=payload.get("error"))
    return http._json_response(status, payload)


def trigger_sample(event, operator, visible=None):
    """The workflow's own last trigger input, for the inspector's template
    autofill: the newest run's recorded input, else the trigger-discovery
    sample for its connector (runs.api_trigger_sample, shared verbatim with
    the agent route the CLI calls). ``visible`` hides a workflow the caller
    may not see behind the same 404 an unknown one gets."""
    query = event.get("queryStringParameters") or {}
    status, payload = runs.api_trigger_sample(
        query.get("workflow") or query.get("workflow_id"), visible=visible)
    session._audit_event(
        str(query.get("workflow") or query.get("workflow_id") or "unknown"),
        "triggers.sample", operator or "unknown",
        outcome="ok" if status == 200 else "error", error=payload.get("error"))
    return http._json_response(status, payload)

