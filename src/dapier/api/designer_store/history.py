"""Version history: the revision list, the two-revision diff, and
rollback."""
import difflib

from ...triggers import published_workflows
from .validation import AUTO_PAUSE_KEYS, FILE_PATTERN, shared
from .drafts import _draft_view, api_save
from .listing import _read_denied, api_get, workflow_yaml_text

RUN_STATE_KEYS = ("run_state", "last_run", "last_run_at", "run_count", "stats",
                  *AUTO_PAUSE_KEYS)


def api_versions(source, visible=None):
    """Version history for one workflow, newest revision first.

    Every save, toggle, and rollback publishes a version record; this lists
    them with who published each one, when, and why, and flags the live
    revision as ``current``. A deleted workflow's history survives the
    delete (the records outlive the YAML and the live item), so the list
    still answers for the id — nothing flagged current, revision 0.
    ``visible`` (G17 auth.visibility, None = unrestricted) hides a workflow
    the caller may not see behind the same 404 an unknown file gets; a
    deleted workflow (nothing stored) stays answerable, like every
    no-owner item.
    """
    if not published_workflows.configured():
        return 503, {"error": "published workflows are not configured"}
    if not FILE_PATTERN.fullmatch(source or ""):
        return 400, {"error": f"invalid workflow file name: {source!r}"}
    if _read_denied(source, visible):
        return 404, {"error": f"no such workflow: {source}"}
    status, payload = api_get(source)
    if status == 200:
        workflow_id = str(payload["workflow"]["id"])
    elif status == 404:
        workflow_id = source.removesuffix(".yaml")
    else:
        return status, payload
    live = published_workflows.get_item(workflow_id) or {}
    live_revision = int(live.get("revision") or 0)
    versions = [{
        "revision": int(version.get("revision") or 0),
        "published_by": version.get("published_by") or "",
        "owner": published_workflows.resolve_owner(version),
        "published_at": version.get("published_at") or "",
        "cause": version.get("cause") or "save",
        "enabled": bool(version.get("enabled", True)),
        "current": int(version.get("revision") or 0) == live_revision,
    } for version in published_workflows.list_versions(workflow_id)]
    payload = {
        "workflow": workflow_id,
        "file": source,
        "revision": live_revision,
        "versions": versions,
    }
    try:
        draft = published_workflows.get_draft(workflow_id)
    except Exception:  # noqa: BLE001 — the history list must not fail on a draft hiccup
        draft = None
    if draft and isinstance(draft.get("workflow"), dict):
        # The unpublished edit sitting on top of the history: stale means the
        # live definition moved past the draft's base (toggle/rollback/
        # auto-pause raced), so publish would refuse it until a fresh save.
        payload["draft"] = _draft_view(draft, live_revision)
    return 200, payload


# The unified diff body is capped so a pathological revision pair cannot
# flood a console dialog or a Lambda response; past the cap the text is cut
# and ``truncated`` says so.
MAX_DIFF_CHARS = 20000


def api_diff(source, from_revision, to_revision, visible=None):
    """Unified text diff between two published versions of one workflow —
    the rollback-confidence half of the versions list.

    Answers ``{file, workflow, from: {revision, yaml}, to: {revision, yaml},
    diff, same, truncated}`` where the YAML texts are the same canonical
    dumps api_get renders and ``diff`` is a plain unified diff between them
    (empty when the definitions are identical, which ``same`` flags — a
    re-save without changes, or the same revision on both sides). Like
    api_versions, it still answers for a deleted workflow's id: the version
    records outlive the live item. ``visible`` (G17 auth.visibility, None =
    unrestricted) hides a workflow the caller may not see behind the same
    404 an unknown file gets.
    """
    if not published_workflows.configured():
        return 503, {"error": "published workflows are not configured"}
    if not FILE_PATTERN.fullmatch(source or ""):
        return 400, {"error": f"invalid workflow file name: {source!r}"}
    if _read_denied(source, visible):
        return 404, {"error": f"no such workflow: {source}"}
    try:
        revisions = (int(from_revision), int(to_revision))
    except (TypeError, ValueError):
        return 400, {"error": "from and to must be version numbers, "
                              "as shown by the versions list"}
    status, payload = api_get(source)
    if status == 200:
        workflow_id = str(payload["workflow"]["id"])
    elif status == 404:
        workflow_id = source.removesuffix(".yaml")
    else:
        return status, payload
    pair = published_workflows.diff_versions(workflow_id, *revisions)
    if pair is None:
        return 404, {"error": f"no version {revisions[0]} or version {revisions[1]} "
                              f"of workflow {workflow_id}"}
    for side, revision in zip(("from", "to"), revisions):
        if not isinstance(pair[side].get("workflow"), dict):
            return 404, {"error": f"no version {revision} of workflow {workflow_id}"}
    from_text = workflow_yaml_text(pair["from"]["workflow"])
    to_text = workflow_yaml_text(pair["to"]["workflow"])
    diff = "".join(difflib.unified_diff(
        from_text.splitlines(keepends=True), to_text.splitlines(keepends=True),
        fromfile=f"{source} v{revisions[0]}", tofile=f"{source} v{revisions[1]}",
    ))
    return 200, {
        "file": source,
        "workflow": workflow_id,
        "from": {"revision": revisions[0], "yaml": from_text},
        "to": {"revision": revisions[1], "yaml": to_text},
        "diff": diff[:shared("MAX_DIFF_CHARS")],
        "same": from_text == to_text,
        "truncated": len(diff) > shared("MAX_DIFF_CHARS"),
    }


def api_rollback(source, body, operator=None):
    """Restore an old version: re-commit its YAML through the save path (so
    git agrees with live again) and publish it as the next revision, recorded
    in the history with cause "rollback" like any other change. A body
    without ``revision`` restores the version before the live one."""
    if not isinstance(body, dict):
        return 400, {"error": "request body must be an object"}
    raw = body.get("revision")
    try:
        if raw is not None and (isinstance(raw, bool) or not isinstance(raw, (int, str))):
            raise ValueError
        revision = int(raw) if raw is not None else None
    except ValueError:
        return 400, {"error": 'body must include "revision": the version number to restore'}
    if not published_workflows.configured():
        return 503, {"error": "published workflows are not configured"}
    if not FILE_PATTERN.fullmatch(source or ""):
        return 400, {"error": f"invalid workflow file name: {source!r}"}
    status, payload = api_get(source)
    if status != 200:
        return status, payload
    workflow = payload["workflow"]
    if revision is None:
        # No revision named: restore the one before the live revision.
        live_revision = int((published_workflows.get_item(workflow["id"]) or {}).get("revision") or 0)
        if live_revision < 2:
            return 409, {"error": f"no version before v{live_revision or 1} to roll back to"}
        revision = live_revision - 1
    version = published_workflows.get_version(workflow["id"], revision)
    if not version or not isinstance(version.get("workflow"), dict):
        return 404, {"error": f"no version {revision} of workflow {workflow['id']}"}
    restored = {
        key: value for key, value in version["workflow"].items()
        if key not in RUN_STATE_KEYS
    }
    restored["enabled"] = bool(version.get("enabled", True))
    yaml_text = workflow_yaml_text(restored)
    # Rollback is a live verb: it publishes (and so stales any draft), it
    # does not draft.
    return api_save(
        {"yaml": yaml_text},
        operator=operator,
        cause="rollback",
        message=f"designer: rollback workflow {workflow['id']} to v{revision}",
        live=True,
    )


# Per-run bookkeeping a stored workflow could carry; a copy starts fresh.
# Everything else — including action step ids — is part of the definition and
# is copied verbatim. The auto-pause keys are runtime state the engine stamps
# (and the enable toggle clears), so a duplicate or rollback never inherits
# a pause.
