"""The draft lifecycle: gated save, publish, discard, and the draft
diff the publish confirmation shows."""
import difflib
import os
import re

import yaml

from ...auth import visibility
from ...triggers import published_workflows
from .github import SyncConfigError, SyncError, TOKEN_SECRET_ENV
from .validation import (FILE_PATTERN, WorkflowError, _LateBinding, shared,
                         parse_workflow)
from .listing import _read_denied, workflow_yaml_text


def save_gate_ids(body):
    """The workflow ids a save-request body would touch, for the G17
    owner-or-operator write gate: the parsed ``yaml``'s id, plus the
    ``renameFrom`` file's id when it names another workflow (renaming is a
    write to the renamed workflow too — promoting the draft would unpublish
    it). Best-effort: a body with no parseable id yields nothing to gate,
    and api_save answers its own 400 for that."""
    if not isinstance(body, dict):
        return []
    ids = []
    yaml_text = body.get("yaml")
    if isinstance(yaml_text, str):
        try:
            workflow = yaml.safe_load(yaml_text)
        except yaml.YAMLError:
            workflow = None
        if isinstance(workflow, dict) and isinstance(workflow.get("id"), str) \
                and workflow["id"].strip():
            ids.append(workflow["id"].strip())
    rename_from = body.get("renameFrom")
    if isinstance(rename_from, str) and FILE_PATTERN.fullmatch(rename_from):
        renamed = rename_from.removesuffix(".yaml")
        if renamed and renamed not in ids:
            ids.append(renamed)
    return ids


def api_save(body, operator=None, cause="save", message=None, live=False):
    """Validate a workflow and store it; a save drafts, a publish goes live.

    The designer's save (the default) writes a *draft*: the parsed definition
    lands as the workflow's ``<id>#draft`` item with the live revision it was
    edited against — nothing publishes, no git commit, no YouTube reconcile,
    so a draft-only workflow fires nothing and the live definition keeps
    running. The publish route promotes the draft through this same function
    with ``live=True`` (cause "publish"), which is also what rollback,
    duplicate, and template-apply pass: those are live verbs and publish as
    before — validated YAML into the published store, a version record, a
    best-effort git commit, and the YouTube subscription reconcile.
    """
    if not isinstance(body, dict):
        return 400, {"error": "request body must be an object"}
    yaml_text = body.get("yaml")
    rename_from = body.get("renameFrom")
    if rename_from is not None and not isinstance(rename_from, str):
        return 400, {"error": "renameFrom must be a file name"}
    if rename_from and not FILE_PATTERN.fullmatch(rename_from):
        return 400, {"error": f"invalid workflow file name: {rename_from!r}"}
    if not published_workflows.configured():
        return 503, {"error": "published workflows are not configured"}
    try:
        workflow = parse_workflow(yaml_text)
    except WorkflowError as exc:
        return 400, {"error": str(exc)}
    if not live:
        previous = published_workflows.get_item(workflow["id"]) or {}
        try:
            published_workflows.save_draft(
                workflow, base_revision=int(previous.get("revision") or 0),
                operator=operator,
                rename_from=rename_from if rename_from else None)
        except Exception as exc:
            return 502, {"error": f"draft save failed: {exc}"}
        base_revision = int(previous.get("revision") or 0)
        return 200, {
            "file": f"{workflow['id']}.yaml",
            "published": False,
            "draft": {
                "base_revision": base_revision,
                "stale": False,
            },
        }
    try:
        previous = published_workflows.get_item(workflow["id"])
        item = published_workflows.publish(workflow, operator=operator,
                                           previous=previous, cause=cause)
        if rename_from and rename_from != f"{workflow['id']}.yaml":
            published_workflows.unpublish(rename_from.removesuffix(".yaml"))
    except Exception as exc:
        return 502, {"error": f"publish failed: {exc}"}
    result = {"file": f"{workflow['id']}.yaml", "published": True,
              "revision": item["revision"]}
    warnings = _sync_youtube(previous=(previous or {}).get("workflow"), workflow=workflow)
    if warnings:
        result["warnings"] = warnings
    if os.environ.get(TOKEN_SECRET_ENV):
        try:
            result.update(commit_workflow(
                yaml_text, rename_from=rename_from,
                message=message or f"designer: save workflow {workflow['id']}"))
        except (SyncConfigError, SyncError) as exc:
            result["git_sync_error"] = str(exc)
    return 200, result


# The stale-draft guard: a draft records the live revision it was edited
# against; when the live definition has moved past it (a toggle, tags/folder
# edit, rollback, or an engine auto-pause raced the edit), promoting it would
# clobber that change, so publish refuses until a fresh draft is saved.
STALE_DRAFT = "stale"


def _draft_view(draft, live_revision):
    """The draft block clients see: base revision, staleness, when/who."""
    base = int(draft.get("base_revision") or 0)
    return {
        "base_revision": base,
        "stale": base < int(live_revision or 0),
        "updated_at": draft.get("updated_at") or "",
        "drafted_by": draft.get("drafted_by") or "",
    }


def api_publish(source, *, operator=None):
    """Promote a workflow's draft to the live published store.

    The promotion runs through the ordinary save path (api_save, live, cause
    "publish"), so the git commit, the version record, and the YouTube
    reconcile all behave exactly like a publish always has; a draft-only
    workflow becomes v1. Refuses with 409 ``stale`` when the draft's
    base_revision is behind the live revision — a toggle, rollback, or
    auto-pause changed the live definition since the draft was saved — and
    404 when there is no draft. The draft item is removed once it is live.
    """
    if not published_workflows.configured():
        return 503, {"error": "published workflows are not configured"}
    if not FILE_PATTERN.fullmatch(source or ""):
        return 400, {"error": f"invalid workflow file name: {source!r}"}
    workflow_id = source.removesuffix(".yaml")
    draft = published_workflows.get_draft(workflow_id)
    if not draft or not isinstance(draft.get("workflow"), dict):
        return 404, {"error": f"no draft of workflow {workflow_id}"}
    live_revision = int((published_workflows.get_item(workflow_id) or {})
                        .get("revision") or 0)
    base_revision = int(draft.get("base_revision") or 0)
    if base_revision < live_revision:
        return 409, {
            "error": (f"the draft of {workflow_id} is stale: it is based on "
                      f"v{base_revision} but v{live_revision} is live — "
                      "save a fresh draft, then publish"),
            "reason": STALE_DRAFT,
            "base_revision": base_revision,
            "revision": live_revision,
        }
    body = {"yaml": workflow_yaml_text(draft["workflow"])}
    if draft.get("rename_from"):
        body["renameFrom"] = str(draft["rename_from"])
    status, payload = api_save(body, operator=operator, cause="publish",
                               live=True)
    if status == 200:
        try:
            published_workflows.delete_draft(workflow_id)
        except Exception:
            pass  # a leftover draft item is harmless; the live item decided
        payload["published"] = True
    return status, payload


def api_discard(source, *, operator=None):
    """Throw a workflow's draft away; the live definition is untouched."""
    del operator  # recorded by the calling route
    if not published_workflows.configured():
        return 503, {"error": "published workflows are not configured"}
    if not FILE_PATTERN.fullmatch(source or ""):
        return 400, {"error": f"invalid workflow file name: {source!r}"}
    workflow_id = source.removesuffix(".yaml")
    draft = published_workflows.get_draft(workflow_id)
    if not draft:
        return 404, {"error": f"no draft of workflow {workflow_id}"}
    try:
        published_workflows.delete_draft(workflow_id)
    except Exception as exc:
        return 502, {"error": f"discard failed: {exc}"}
    return 200, {"file": source, "workflow_id": workflow_id, "discarded": True}


def api_draft(source, visible=None):
    """One workflow's draft (the designer's load path for a draft-only
    workflow, and the "you have a draft" indicator): the drafted definition,
    its canonical YAML, and the draft block. 404 when nothing is drafted —
    or when ``visible`` (G17 auth.visibility, None = unrestricted) may not
    see the workflow, same answer."""
    if not published_workflows.configured():
        return 503, {"error": "published workflows are not configured"}
    if not FILE_PATTERN.fullmatch(source or ""):
        return 400, {"error": f"invalid workflow file name: {source!r}"}
    workflow_id = source.removesuffix(".yaml")
    if _read_denied(source, visible):
        return 404, {"error": f"no draft of workflow {workflow_id}"}
    draft = published_workflows.get_draft(workflow_id)
    if not draft or not isinstance(draft.get("workflow"), dict):
        return 404, {"error": f"no draft of workflow {workflow_id}"}
    workflow = draft["workflow"]
    live_revision = int((published_workflows.get_item(workflow_id) or {})
                        .get("revision") or 0)
    return 200, {
        "workflow": workflow,
        "published": False,
        "yaml": workflow_yaml_text(workflow),
        "draft": _draft_view(draft, live_revision),
    }


def api_draft_diff(source, visible=None):
    """Draft vs live in the api_diff shape: ``from`` is the live definition
    (revision 0, empty YAML, when the workflow is draft-only — the diff shows
    it all as new), ``to`` is the draft at revision ``"draft"``. ``same``
    flags a draft identical to live (safe to publish; it refreshes nothing).
    ``visible`` (G17 auth.visibility, None = unrestricted) hides both sides
    when the caller may not see the workflow — same 404 as no draft."""
    if not published_workflows.configured():
        return 503, {"error": "published workflows are not configured"}
    if not FILE_PATTERN.fullmatch(source or ""):
        return 400, {"error": f"invalid workflow file name: {source!r}"}
    workflow_id = source.removesuffix(".yaml")
    if _read_denied(source, visible):
        return 404, {"error": f"no draft of workflow {workflow_id}"}
    draft = published_workflows.get_draft(workflow_id)
    if not draft or not isinstance(draft.get("workflow"), dict):
        return 404, {"error": f"no draft of workflow {workflow_id}"}
    live = published_workflows.get_item(workflow_id) or {}
    live_revision = int(live.get("revision") or 0)
    live_workflow = live.get("workflow")
    from_text = (workflow_yaml_text(live_workflow)
                 if isinstance(live_workflow, dict) else "")
    to_text = workflow_yaml_text(draft["workflow"])
    diff = "".join(difflib.unified_diff(
        from_text.splitlines(keepends=True), to_text.splitlines(keepends=True),
        fromfile=f"{source} v{live_revision}" if live_revision
        else f"{source} (nothing live)",
        tofile=f"{source} draft",
    ))
    return 200, {
        "file": source,
        "workflow": workflow_id,
        "from": {"revision": live_revision, "yaml": from_text},
        "to": {"revision": "draft", "yaml": to_text},
        "diff": diff[:shared("MAX_DIFF_CHARS")],
        "same": from_text == to_text,
        "truncated": len(diff) > shared("MAX_DIFF_CHARS"),
    }




# The git-sync seams resolve through the package at call time, so tests
# patching designer_store.<name> reach every caller; the facade re-binds
# the real implementations after the star imports.
commit_workflow = _LateBinding("commit_workflow")
_sync_youtube = _LateBinding("_sync_youtube")
