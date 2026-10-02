"""Workflow designer API: drafts, publish and rollback, versions,
tags and folders, export, and test runs, over the CLI's
bearer authentication with the owner-or-operator write gate."""

import json

from .. import designer_store

from .common import _json_response, _no_store, _visibility, _write_denied, _save_denied

from .common import _LateBinding

# Shared dependencies resolved through the agent package at call time:
# tests patch agent.<name> and every route module must see the patch.
authenticate = _LateBinding("authenticate")
require_operator = _LateBinding("require_operator")
_is_operator = _LateBinding("_is_operator")
_tables = _LateBinding("_tables")
audit = _LateBinding("audit")
verify_id_token = _LateBinding("verify_id_token")


__all__ = ["designer_api", "designer_bulk_api", "designer_delete_api", "designer_discard_api", "designer_draft_api", "designer_draft_diff_api", "designer_duplicate_api", "designer_export_all_api", "designer_export_api", "designer_folder_api", "designer_publish_api", "designer_rollback_api", "designer_tags_api", "designer_test_api", "designer_test_step_api", "designer_toggle_api", "designer_versions_api", "designer_versions_diff_api"]



def designer_api(event, method, source=None):
    """Operator-only workflow designer API over the CLI's bearer authentication."""
    subject, error = require_operator(event, "workflow.save")
    if error:
        return error
    if method == "GET":
        if source:
            status, payload = designer_store.api_get(
                source, visible=_visibility(event, subject))
        else:
            query = event.get("queryStringParameters") or {}
            status, payload = designer_store.api_list(query.get("q") or None,
                                                      tag=query.get("tag") or None,
                                                      folder=query.get("folder") or None,
                                                      visible=_visibility(event, subject))
        return _json_response(status, payload)
    try:
        body = json.loads(event.get("body") or "{}")
        denied = _save_denied(event, subject, body)
        if denied:
            return denied
        status, payload = designer_store.api_save(body, operator=subject)
    except (ValueError, json.JSONDecodeError) as exc:
        return _json_response(400, {"error": str(exc) or "Invalid request"})
    audit.emit(str(payload.get("file", "unknown")), "workflow.save", subject,
               outcome="ok" if status == 200 else "error")
    return _json_response(status, payload)


def designer_export_all_api(event):
    """Operator-only export-all: every workflow's canonical YAML as one zip.

    Same domain function as /api/admin/designer/workflows/export-all — the
    zip is built once server-side and ships base64 in the JSON body, so the
    CLI never assembles or renders YAML itself. The export itself is audited,
    mirroring the audit CSV export: bulk reads leave a mark in the trail;
    denials are recorded by require_operator.
    """
    subject, error = require_operator(event, "workflow.export-all")
    if error:
        return error
    status, payload = designer_store.api_export_all(
        visible=_visibility(event, subject))
    if status == 200:
        audit.emit("workflows", "workflow.export-all", subject, outcome="ok")
    return _no_store(_json_response(status, payload))


def designer_export_api(event):
    """Operator-only workflow bundle: every workflow's canonical YAML as one
    zip, narrowed by the optional ``?tag=`` / ``?folder=`` (the designer
    list's filters). `workflows export --all` drives this.

    Same domain function as /api/admin/designer/export — the zip (one
    canonical YAML per workflow plus a manifest.json) is built once
    server-side and ships base64 in the JSON body with the attachment
    content-disposition naming it, so the CLI only decodes and writes. The
    export itself is audited, mirroring the audit CSV export: bulk reads
    leave a mark in the trail; denials are recorded by require_operator.
    """
    subject, error = require_operator(event, "workflow.export")
    if error:
        return error
    query = event.get("queryStringParameters") or {}
    status, payload = designer_store.api_export(tag=query.get("tag"),
                                                folder=query.get("folder"),
                                                visible=_visibility(event, subject))
    if status == 200:
        audit.emit("workflows", "workflow.export", subject, outcome="ok")
        return _no_store(_json_response(status, payload, headers={
            "content-disposition": f'attachment; filename="{payload["filename"]}"'}))
    return _no_store(_json_response(status, payload))


def designer_toggle_api(event, source):
    """Operator-only live enable/disable; mirrors the console's toggle."""
    subject, error = require_operator(event, "workflow.toggle")
    if error:
        return error
    denied = _write_denied(event, subject, str(source).removesuffix(".yaml"),
                           "workflow.toggle")
    if denied:
        return denied
    try:
        body = json.loads(event.get("body") or "{}")
    except (ValueError, json.JSONDecodeError) as exc:
        return _json_response(400, {"error": str(exc) or "Invalid request"})
    status, payload = designer_store.api_toggle(source, body, operator=subject)
    audit.emit(str(source), "workflow.toggle", subject,
               outcome="ok" if status == 200 else "error")
    return _json_response(status, payload)


def designer_duplicate_api(event, source):
    """Operator-only workflow copy: a new id/file through the same
    commit-and-publish path as a save; the original is untouched. Mirrors the
    console's duplicate endpoint."""
    subject, error = require_operator(event, "workflow.duplicate")
    if error:
        return error
    try:
        body = json.loads(event.get("body") or "{}")
        status, payload = designer_store.api_duplicate(source, body, operator=subject)
    except (ValueError, json.JSONDecodeError) as exc:
        return _json_response(400, {"error": str(exc) or "Invalid request"})
    audit.emit(str(payload.get("file", source or "unknown")), "workflow.duplicate", subject,
               outcome="ok" if status == 200 else "error", error=payload.get("error"))
    return _json_response(status, payload)


def designer_delete_api(event, source):
    """Operator-only workflow delete: unpublish live, then remove the YAML
    from the repo in one git commit. Refused while runs of the workflow are
    parked on a delay. Mirrors the console's delete endpoint."""
    subject, error = require_operator(event, "workflow.delete")
    if error:
        return error
    denied = _write_denied(event, subject, str(source).removesuffix(".yaml"),
                           "workflow.delete")
    if denied:
        return denied
    status, payload = designer_store.api_delete(source)
    audit.emit(str(source), "workflow.delete", subject,
               outcome="ok" if status == 200 else "error", error=payload.get("error"))
    return _json_response(status, payload)


def designer_tags_api(event, source):
    """Operator-only tags editor: replace a workflow's tag set (Zapier-style
    organization). Mirrors the console's tags endpoint."""
    subject, error = require_operator(event, "workflow.tags")
    if error:
        return error
    denied = _write_denied(event, subject, str(source).removesuffix(".yaml"),
                           "workflow.tags")
    if denied:
        return denied
    try:
        body = json.loads(event.get("body") or "{}")
        status, payload = designer_store.api_tags(source, body, operator=subject)
    except (ValueError, json.JSONDecodeError) as exc:
        return _json_response(400, {"error": str(exc) or "Invalid request"})
    audit.emit(str(payload.get("file", source or "unknown")), "workflow.tags", subject,
               outcome="ok" if status == 200 else "error", error=payload.get("error"))
    return _json_response(status, payload)


def designer_folder_api(event, source):
    """Operator-only folder editor: put a workflow in a Zapier-style folder
    (flat — at most one per workflow, an empty string clears it). Mirrors the
    console's folder endpoint."""
    subject, error = require_operator(event, "workflow.folder")
    if error:
        return error
    denied = _write_denied(event, subject, str(source).removesuffix(".yaml"),
                           "workflow.folder")
    if denied:
        return denied
    try:
        body = json.loads(event.get("body") or "{}")
        status, payload = designer_store.api_folder(source, body, operator=subject)
    except (ValueError, json.JSONDecodeError) as exc:
        return _json_response(400, {"error": str(exc) or "Invalid request"})
    audit.emit(str(payload.get("file", source or "unknown")), "workflow.folder", subject,
               outcome="ok" if status == 200 else "error", error=payload.get("error"))
    return _json_response(status, payload)


def designer_bulk_api(event):
    """Operator-only bulk enable/disable over several workflows at once
    (`dapier workflows on|off a.yaml b.yaml`, the console's selection bar).

    Each id toggles through the same api_toggle semantics and answers per id;
    one audit row covers the batch, with the id list as the subject. The G17
    write gate rides along per id (api_bulk's ``visible``): a foreign id
    fails alone, and the tag/search/all scopes only ever target what the
    caller can see."""
    subject, error = require_operator(event, "workflow.bulk-toggle")
    if error:
        return error
    try:
        body = json.loads(event.get("body") or "{}")
        status, payload = designer_store.api_bulk(
            body, operator=subject, visible=_visibility(event, subject))
    except (ValueError, json.JSONDecodeError) as exc:
        return _json_response(400, {"error": str(exc) or "Invalid request"})
    ids = [str(item) for item in (body or {}).get("ids") or []] if isinstance(body, dict) else []
    batch = ", ".join(ids)
    if len(batch) > 400:
        batch = batch[:400] + f" … (+{len(ids)} total)"
    audit.emit(batch or "bulk", "workflow.bulk-toggle", subject,
               outcome="ok" if status == 200 else "error", error=payload.get("error"))
    return _no_store(_json_response(status, payload))


def designer_versions_api(event, source):
    """Operator-only version history: what was published, when, by whom, why."""
    subject, error = require_operator(event, "workflow.versions")
    if error:
        return error
    status, payload = designer_store.api_versions(
        source, visible=_visibility(event, subject))
    return _json_response(status, payload)


def designer_versions_diff_api(event, source):
    """Operator-only version diff: the unified diff between two revisions
    (the rollback-confidence half of the versions list). Mirrors the
    console's diff endpoint; like the versions read it is not itself
    audited — require_operator records the denials."""
    subject, error = require_operator(event, "workflow.versions")
    if error:
        return error
    query = event.get("queryStringParameters") or {}
    status, payload = designer_store.api_diff(source, query.get("from"),
                                              query.get("to"),
                                              visible=_visibility(event, subject))
    return _json_response(status, payload)


def designer_rollback_api(event, source):
    """Operator-only rollback: republish an old version as the next revision."""
    subject, error = require_operator(event, "workflow.rollback")
    if error:
        return error
    denied = _write_denied(event, subject, str(source).removesuffix(".yaml"),
                           "workflow.rollback")
    if denied:
        return denied
    try:
        body = json.loads(event.get("body") or "{}")
        status, payload = designer_store.api_rollback(source, body, operator=subject)
    except (ValueError, json.JSONDecodeError) as exc:
        return _json_response(400, {"error": str(exc) or "Invalid request"})
    audit.emit(str(payload.get("file", source or "unknown")), "workflow.rollback", subject,
               outcome="ok" if status == 200 else "error", error=payload.get("error"))
    return _json_response(status, payload)


# ---- Draft vs live (G15): `workflows save` drafts; these promote or throw ----

def designer_publish_api(event, source):
    """Operator-only draft promotion: the draft goes live through the
    ordinary publish path (git, revision, YouTube reconcile). 409 stale when
    the live definition moved past the draft's base revision. Mirrors the
    console's publish endpoint."""
    subject, error = require_operator(event, "workflow.publish")
    if error:
        return error
    denied = _write_denied(event, subject, str(source).removesuffix(".yaml"),
                           "workflow.publish")
    if denied:
        return denied
    status, payload = designer_store.api_publish(source, operator=subject)
    audit.emit(str(payload.get("file", source or "unknown")), "workflow.publish", subject,
               outcome="ok" if status == 200 else "error", error=payload.get("error"))
    return _json_response(status, payload)


def designer_discard_api(event, source):
    """Operator-only draft discard: throw the draft away, live untouched.
    Mirrors the console's discard endpoint."""
    subject, error = require_operator(event, "workflow.discard")
    if error:
        return error
    denied = _write_denied(event, subject, str(source).removesuffix(".yaml"),
                           "workflow.discard")
    if denied:
        return denied
    status, payload = designer_store.api_discard(source, operator=subject)
    audit.emit(str(source), "workflow.discard", subject,
               outcome="ok" if status == 200 else "error", error=payload.get("error"))
    return _json_response(status, payload)


def designer_draft_api(event, source):
    """Operator-only draft read: the drafted definition and its base revision
    (the CLI's `workflows show --draft` view of a draft-only workflow). Like
    the versions read it is not itself audited — require_operator records
    the denials."""
    subject, error = require_operator(event, "workflow.versions")
    if error:
        return error
    status, payload = designer_store.api_draft(
        source, visible=_visibility(event, subject))
    return _json_response(status, payload)


def designer_draft_diff_api(event, source):
    """Operator-only draft diff: draft vs live in the versions-diff shape.
    Mirrors the console's draft-diff endpoint; not itself audited."""
    subject, error = require_operator(event, "workflow.versions")
    if error:
        return error
    status, payload = designer_store.api_draft_diff(
        source, visible=_visibility(event, subject))
    return _json_response(status, payload)


def designer_test_api(event, source):
    """Operator-only test run: dry-run a workflow on a sample event, or run
    it for real with execute. Mirrors the console's test endpoint."""
    subject, error = require_operator(event, "workflow.test")
    if error:
        return error
    if source:
        # Keyed by a saved workflow: the write gate applies. An inline
        # workflow (the designer's unsaved draft) is nobody's stored row.
        denied = _write_denied(event, subject,
                               str(source).removesuffix(".yaml"), "workflow.test")
        if denied:
            return denied
    try:
        body = json.loads(event.get("body") or "{}")
        status, payload = designer_store.api_test_run(source, body, operator=subject)
    except (ValueError, json.JSONDecodeError) as exc:
        return _json_response(400, {"error": str(exc) or "Invalid request"})
    audit.emit(str(payload.get("file", source or "unknown")), "workflow.test", subject,
               outcome="ok" if status == 200 else "error", error=payload.get("error"))
    return _json_response(status, payload)


def designer_test_step_api(event, source):
    """Operator-only per-step test: run one action against a sample event —
    for real with execute, side effects limited to that step. Mirrors the
    console's test-step endpoint; same workflow.test grant as the whole-run
    test, since it is the same capability at step granularity."""
    subject, error = require_operator(event, "workflow.test")
    if error:
        return error
    if source:
        # Keyed by a saved workflow: the write gate applies. An inline
        # workflow (the designer's unsaved draft) is nobody's stored row.
        denied = _write_denied(event, subject,
                               str(source).removesuffix(".yaml"), "workflow.test")
        if denied:
            return denied
    try:
        body = json.loads(event.get("body") or "{}")
        status, payload = designer_store.api_test_step(source, body, operator=subject)
    except (ValueError, json.JSONDecodeError) as exc:
        return _json_response(400, {"error": str(exc) or "Invalid request"})
    audit.emit(str(payload.get("file", source or "unknown")), "workflow.test-step", subject,
               outcome="ok" if status == 200 else "error", error=payload.get("error"))
    return _json_response(status, payload)
