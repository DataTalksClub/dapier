"""Listing and reads: the workflow summaries, the single-workflow get
with its visibility gate, and the private projection helpers the other
domains share."""
import base64
import io
import json
import re
import zipfile
from datetime import datetime, timezone

import yaml

from ...auth import visibility
from ...triggers import hook_triggers, published_workflows
from .github import sync_status
from .validation import FILE_PATTERN, ordered_workflow, parse_workflow


def _tags_of(workflow):
    """A workflow's tags as a clean list, defensively: hand-written YAML can
    carry anything under ``tags`` — a string, a list with blanks — and the
    list views must degrade to [] rather than fail the whole page. The strict
    validation lives where tags are written (parse_workflow, api_tags)."""
    tags = workflow.get("tags") if isinstance(workflow, dict) else None
    if not isinstance(tags, list):
        return []
    return [str(tag).strip() for tag in tags if isinstance(tag, str) and str(tag).strip()]


def _folder_of(workflow):
    """A workflow's folder as a clean string ("" when none), defensively:
    hand-written YAML can carry anything under ``folder`` and the list views
    must degrade rather than fail the whole page. Strict validation lives
    where the folder is written (parse_workflow, api_folder)."""
    folder = workflow.get("folder") if isinstance(workflow, dict) else None
    return folder.strip() if isinstance(folder, str) else ""


def _summary(workflow, source):
    """One list row: primary trigger, how many there are, and the effective actions."""
    from ...engine import matching

    triggers = matching.workflow_triggers(workflow)
    primary = triggers[0] if triggers else {}
    actions = workflow.get("actions")
    if workflow.get("flow"):
        actions = matching.flow_actions(workflow["flow"])
    return {
        "id": str(workflow["id"]),
        "enabled": workflow.get("enabled", True),
        "description": str(workflow.get("description") or ""),
        "source": source,
        "connector": str(primary.get("connector", "?")),
        "event": str(primary.get("event", "?")),
        "triggerCount": len(triggers),
        "actionCount": len(actions or []),
        # Step types (flow-resolved) for the list's ?q= search and clients.
        "actionTypes": [str(action.get("type") or "") for action in (actions or [])
                        if isinstance(action, dict)],
        # Zapier-style organization labels; set through api_tags.
        "tags": _tags_of(workflow),
        # Zapier-style flat folder ("" when none); set through api_folder.
        "folder": _folder_of(workflow),
        # Paused by the engine after consecutive failed runs (cleared by
        # re-enabling — the resume verb); engine-stamped runtime state.
        "auto_paused": workflow.get("auto_paused") is True,
    }


def _published_by_file(source):
    """The published item for one file name, or None (invalid name / not published)."""
    if not published_workflows.configured() or not FILE_PATTERN.fullmatch(source or ""):
        return None
    return published_workflows.get_item(source.removesuffix(".yaml"))


def api_list(q=None, tag=None, folder=None, visible=None):
    """Managed workflows in the live published store.

    A workflow saved but not yet picked up by the deploy pipeline shows up
    here too — its published state is what actually runs. ``q`` filters
    case-insensitively over each row's id, description, trigger connector
    and event, action step types, tags, and folder. ``tag`` narrows to
    workflows carrying exactly that tag (case-insensitive) — Zapier's tag
    view. ``folder`` narrows to workflows sitting in exactly that folder
    (case-insensitive) — Zapier's folder view.

    ``visible`` (an auth.visibility.Visibility, None = unrestricted — the
    bulk-toggle scope) read-filters the list, G17 Phase 2: a non-operator
    sees the workflows it owns — live rows by their owner, draft-only rows
    by their drafted_by — and unowned workflows stay visible to everyone. A
    hidden live item hides its draft pair too (one workflow, one
    visibility); the write-side gate is visibility.ensure_can_write
    (Phase 3), applied by the route layers.
    """
    summaries = {}
    drafts = {}
    if published_workflows.configured():
        hidden = set()
        for item in published_workflows.load_items(include_drafts=True):
            if item.get("draft_of"):
                drafts[str(item["draft_of"])] = item
                continue
            workflow = item.get("workflow")
            if not isinstance(workflow, dict) or not workflow.get("id"):
                continue
            if visible is not None and not visible.owner_visible(
                    visibility.owner_of_item(item)):
                # Not this caller's workflow — remember the id so its draft
                # pair (collected below) dies with the row.
                hidden.add(str(workflow["id"]))
                continue
            summary = _summary(workflow, item.get("file") or f"{workflow['id']}.yaml")
            # Informational G17 owner (resolved on read; published_by before
            # the stamp) — Phase 2 read-filters this list by it.
            summaries[summary["id"]] = {**summary, "published": True,
                                        "owner": item.get("owner") or ""}
        # A workflow with a draft but nothing live still lists — Zapier shows
        # the unpublished draft in the sidebar — flagged published: false so
        # nobody mistakes it for running state. Drafts never fire: the engine
        # reads the draft-blind loader.
        for workflow_id, item in drafts.items():
            workflow = item.get("workflow")
            if not isinstance(workflow, dict) or not workflow.get("id"):
                continue
            if workflow_id in hidden:
                continue
            if visible is not None and not visible.owner_visible(
                    visibility.owner_of_item(item)):
                continue
            if workflow_id in summaries:
                summaries[workflow_id]["has_draft"] = True
                continue
            summary = _summary(workflow, item.get("file") or f"{workflow_id}.yaml")
            # Draft-only rows are owned by their drafted_by (visibility.
            # owner_of_item) — exposed informationally like the live rows.
            summaries[str(workflow["id"])] = {**summary, "published": False,
                                              "owner": visibility.owner_of_item(item)}
    for workflow, owner in hook_triggers.listed_workflows(visible):
        summaries.setdefault(str(workflow["id"]), {
            **_summary(workflow, None), "published": True, "owner": owner,
        })
    ordered = sorted(summaries.values(), key=lambda summary: summary["id"])
    search = str(q or "").strip().lower()
    if search:
        ordered = [
            summary for summary in ordered
            if search in " ".join(
                [summary["id"], summary.get("description") or "",
                 summary.get("connector") or "", summary.get("event") or "",
                 *(summary.get("actionTypes") or []),
                 *(summary.get("tags") or []),
                 summary.get("folder") or ""]).lower()
        ]
    wanted_tag = str(tag or "").strip().lower()
    if wanted_tag:
        ordered = [summary for summary in ordered
                   if wanted_tag in {existing.lower() for existing in summary.get("tags") or []}]
    wanted_folder = str(folder or "").strip().lower()
    if wanted_folder:
        ordered = [summary for summary in ordered
                   if str(summary.get("folder") or "").strip().lower() == wanted_folder]
    return 200, {"workflows": ordered, **sync_status()}




def workflow_yaml_text(workflow):
    """The canonical YAML text for a stored definition — the same dump the
    save/toggle/duplicate/rollback paths write, so an export re-saves
    byte-identical."""
    return yaml.safe_dump(ordered_workflow(workflow), sort_keys=False)


def _read_denied(source, visible):
    """The G17 read rule for one file-keyed read: True when ``visible`` may
    not see this workflow. The owner resolves from the live item, else the
    draft row (a draft-only workflow's owner, the write gate's resolution);
    nothing stored under the id is unclaimed and stays visible — the
    read-side default that never hides data, so a deleted workflow's
    surviving version history reads like every no-owner item."""
    if visible is None:
        return False
    item = _published_by_file(source)
    if item is None:
        try:
            item = published_workflows.get_draft(
                str(source or "").removesuffix(".yaml"))
        except Exception:  # noqa: BLE001 — the gate must not fail the read
            return False
    if item is None:
        return False
    return not visible.owner_visible(visibility.owner_of_item(item))


def api_get(source, visible=None):
    """One workflow from the live published store.

    The payload carries ``yaml``, the canonical text rendered from the stored
    definition, so `workflows export` and the console's download round-trip
    through `workflows save` without re-rendering client-side. Draft state is
    not served here (this is the live read — the toggle/tags/folder/delete
    verbs resolve through it); the list rows carry ``has_draft`` /
    ``published: false`` and the versions list carries the ``draft`` block.
    ``visible`` (G17 auth.visibility, None = unrestricted) scopes the read:
    a workflow the caller may not see answers exactly like a missing one.
    """
    item = _published_by_file(source)
    if visible is not None and item is not None and not visible.owner_visible(
            visibility.owner_of_item(item)):
        item = None
    if item and isinstance(item.get("workflow"), dict):
        return 200, {"workflow": item["workflow"], "published": True,
                     "yaml": workflow_yaml_text(item["workflow"])}
    return 404, {"error": f"no such workflow: {source}"}

