"""Designer workflow export endpoints (single and all)."""
from .. import designer_store
from ... import http
from ...auth import session


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


