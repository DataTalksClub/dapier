"""Designer workflow endpoints: the CRUD and organization
wrappers over designer_store, plus the draft lifecycle and dry runs."""
from ... import copilot
from .. import designer_store
from ... import http
import json
from ...auth import session
from .gates import _save_denied, _write_denied


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

