"""Organization: tags, folders, and the bulk enable/disable."""
from ...auth import visibility
from ...triggers import published_workflows
from .github import SyncConfigError, SyncError
from .validation import (FILE_PATTERN, WorkflowError, _LateBinding,
                         _validate_folder, _validate_tags, filename_for,
                         parse_workflow)
from .lifecycle import api_toggle
from .listing import (_folder_of, _tags_of, api_get, api_list,
                      workflow_yaml_text)

MAX_BULK_IDS = 100


def api_tags(source, body, *, operator=None):
    """Edit a workflow's tags (Zapier-style organization labels).

    Body ``{"tags": ["a", "b"]}`` replaces the whole set (an empty list or
    ``--clear`` clears it); ``{"add": [...], "remove": [...]}`` merges —
    removals apply first and win, matching case-insensitively. Tags are
    stored lowercase, deduped, and bounded like any hand-written ``tags:``
    list (parse_workflow enforces the same rules, so a hand edit cannot
    smuggle in what the endpoint would reject). Tags live in the workflow
    YAML, so they survive deploys and travel with save/export/duplicate/
    rollback like any other definition key. Writing them re-publishes the
    definition (cause "tags" in the version history) and commits the updated
    YAML best-effort, exactly like the enable/disable toggle.
    """
    if not isinstance(body, dict):
        return 400, {"error": "request body must be an object"}
    replace = body.get("tags")
    adds = body.get("add")
    removes = body.get("remove")
    if replace is None and adds is None and removes is None:
        return 400, {"error": 'body must be {"tags": [...]} to replace the set '
                              'or {"add": [...], "remove": [...]} to edit it'}
    if replace is not None and (adds is not None or removes is not None):
        return 400, {"error": "send tags to replace the set, or add/remove to edit it — not both"}
    for name, value in (("tags", replace), ("add", adds), ("remove", removes)):
        if value is None:
            continue
        if not isinstance(value, list) or not all(isinstance(entry, str) for entry in value):
            return 400, {"error": f"{name} must be a list of strings"}
    if not published_workflows.configured():
        return 503, {"error": "published workflows are not configured"}
    if not FILE_PATTERN.fullmatch(source or ""):
        return 400, {"error": f"invalid workflow file name: {source!r}"}
    status, payload = api_get(source)
    if status != 200:
        return status, payload
    workflow = payload["workflow"]
    if replace is not None:
        merged = replace
    else:
        dropped = {tag.strip().lower() for tag in removes or [] if tag.strip()}
        merged = [tag for tag in _tags_of(workflow) if tag.lower() not in dropped]
        merged.extend(adds or [])
    try:
        tags = sorted({tag.lower() for tag in _validate_tags(merged)})
    except WorkflowError as exc:
        return 400, {"error": str(exc)}
    if tags:
        workflow["tags"] = tags
    else:
        workflow.pop("tags", None)
    try:
        previous = published_workflows.get_item(workflow["id"])
        published = published_workflows.publish(workflow, operator=operator,
                                                previous=previous, cause="tags")
    except Exception as exc:
        return 502, {"error": f"publish failed: {exc}"}
    result = {
        "file": source,
        "workflow_id": str(workflow["id"]),
        "tags": tags,
        "revision": int(published.get("revision") or 0),
        "published": True,
    }
    try:
        committed = commit_workflow(
            workflow_yaml_text(workflow),
            message=f"designer: tag workflow {workflow['id']}",
        )
        result["commit"] = committed["commit"]
    except (SyncConfigError, SyncError) as exc:
        result["git_sync_error"] = str(exc)
    return 200, result


def api_folder(source, body, *, operator=None):
    """Put a workflow in a Zapier-style folder (flat organization, unlike
    tags: at most one folder per workflow, or none).

    Body ``{"folder": "Name"}`` sets or moves the workflow; ``{"folder": ""}``
    clears it. The value is validated like any hand-written ``folder:`` key
    (parse_workflow enforces the same rules, so a hand edit cannot smuggle in
    what the endpoint would reject): a stripped string, at most
    MAX_FOLDER_LENGTH characters, never containing ``/`` or ``\\`` — a folder
    is a name, not a path. The folder lives in the workflow YAML, so it
    survives deploys and travels with save/export/duplicate/rollback like any
    other definition key. Writing it re-publishes the definition (cause
    "folder" in the version history) and commits the updated YAML
    best-effort, exactly like the tags and enable/disable editors.
    """
    if not isinstance(body, dict):
        return 400, {"error": "request body must be an object"}
    if "folder" not in body:
        return 400, {"error": 'body must be {"folder": "..."} — an empty string clears the folder'}
    try:
        folder = _validate_folder(body.get("folder"))
    except WorkflowError as exc:
        return 400, {"error": str(exc)}
    if not published_workflows.configured():
        return 503, {"error": "published workflows are not configured"}
    if not FILE_PATTERN.fullmatch(source or ""):
        return 400, {"error": f"invalid workflow file name: {source!r}"}
    status, payload = api_get(source)
    if status != 200:
        return status, payload
    workflow = payload["workflow"]
    if folder:
        workflow["folder"] = folder
    else:
        workflow.pop("folder", None)
    try:
        previous = published_workflows.get_item(workflow["id"])
        published = published_workflows.publish(workflow, operator=operator,
                                                previous=previous, cause="folder")
    except Exception as exc:
        return 502, {"error": f"publish failed: {exc}"}
    result = {
        "file": source,
        "workflow_id": str(workflow["id"]),
        "folder": folder,
        "revision": int(published.get("revision") or 0),
        "published": True,
    }
    try:
        committed = commit_workflow(
            workflow_yaml_text(workflow),
            message=(f"designer: move workflow {workflow['id']} to folder {folder!r}"
                     if folder else
                     f"designer: move workflow {workflow['id']} out of its folder"),
        )
        result["commit"] = committed["commit"]
    except (SyncConfigError, SyncError) as exc:
        result["git_sync_error"] = str(exc)
    return 200, result


# The bulk enable/disable cap: one call answers for at most this many
# workflows, so a runaway client cannot enqueue an unbounded toggle batch.


def api_bulk(body, operator=None, visible=None):
    """Enable or disable several workflows in one call.

    Exactly one scope: ``ids`` (explicit file names — `workflows on|off
    a.yaml b.yaml`, the console's shown-rows bulk buttons), ``tag`` (every
    workflow carrying it), ``search`` (the list's ?q= text), or ``all: true``
    (`workflows enable --all`). Each target goes through the same api_toggle
    semantics — live immediately, committed YAML best-effort — and answers on
    its own: an unknown file, an invalid name, or a publish hiccup fails that
    id without stopping the batch, and the response reports it. Returns 200
    with per-target results ``[{id, ok, error?}]``; the calling route writes
    the single audit row for the whole batch.

    ``visible`` (an auth.visibility.Visibility, None = unrestricted) applies
    the G17 write gate per target: the tag/search/all scopes resolve through
    the same read filter as the list (a non-operator only ever targets what
    it can see), and every target is owner-checked before its toggle — a
    foreign id fails alone, like any other per-id error.
    """
    if not isinstance(body, dict):
        return 400, {"error": "request body must be an object"}
    ids = body.get("ids")
    tag = body.get("tag")
    search = body.get("search")
    all_workflows = body.get("all", False)
    action = body.get("action")
    if action not in ("enable", "disable"):
        return 400, {"error": 'body must include "action": "enable" or "disable"'}
    if ids is not None and (not isinstance(ids, list) or not ids
                            or not all(isinstance(item, str) and item.strip() for item in ids)):
        return 400, {"error": '"ids" must be a non-empty list of workflow file names'}
    if tag is not None and not isinstance(tag, str):
        return 400, {"error": '"tag" must be a string'}
    if search is not None and not isinstance(search, str):
        return 400, {"error": '"search" must be a string'}
    if not isinstance(all_workflows, bool):
        return 400, {"error": '"all" must be a boolean'}
    scopes = [name for name, value in (("ids", ids), ("tag", tag), ("search", search)) if value]
    if len(scopes) + (1 if all_workflows else 0) > 1:
        return 400, {"error": "scope the bulk toggle one way: ids, tag, search, or all: true"}
    if not scopes and not all_workflows:
        return 400, {"error": "scope the bulk toggle: ids, tag, search, or all: true"}
    if ids is not None and len(ids) > MAX_BULK_IDS:
        return 400, {"error": f"at most {MAX_BULK_IDS} workflows per bulk call"}
    scope = ("ids" if ids is not None
             else f"tag:{tag.strip()}" if tag is not None
             else f"search:{search.strip()}" if search is not None
             else "all")
    if ids is not None:
        targets = [(raw.strip(), raw.strip()) for raw in ids]
    else:
        status, payload = api_list(q=search, tag=tag, visible=visible)
        if status != 200:
            return status, payload
        targets = [(row.get("source") or filename_for(row["id"]), row["id"])
                   for row in payload["workflows"]]
    enabled = action == "enable"
    results = []
    for source, label in targets:
        denied = (visible.can_write(str(source).removesuffix(".yaml"))
                  if visible is not None else None)
        if denied is not None:
            results.append({"id": label, "ok": False,
                            "error": str(denied[1].get("error") or "denied")})
            continue
        status, payload = api_toggle(source, {"enabled": enabled}, operator=operator)
        if status == 200:
            row = {
                "id": label,
                "ok": True,
                "file": payload.get("file") or source,
                "enabled": bool(payload.get("enabled", enabled)),
                "commit": payload.get("commit"),
            }
            if payload.get("warnings"):
                row["warnings"] = payload["warnings"]
            results.append(row)
        else:
            results.append({
                "id": label,
                "ok": False,
                "error": str(payload.get("error") or f"toggle returned {status}"),
            })
    return 200, {
        "action": action,
        "scope": scope,
        "requested": len(results),
        "ok": sum(1 for result in results if result["ok"]),
        "results": results,
    }




# The git-sync seams resolve through the package at call time, so tests
# patching designer_store.<name> reach every caller; the facade re-binds
# the real implementations after the star imports.
commit_workflow = _LateBinding("commit_workflow")
