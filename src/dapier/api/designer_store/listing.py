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
from ...triggers import published_workflows
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
    """One list row: primary trigger, how many there are, and the actions."""
    from ...engine import matching, naming

    triggers = matching.workflow_triggers(workflow)
    primary = triggers[0] if triggers else {}
    actions = workflow.get("actions")
    return {
        "id": str(workflow["id"]),
        # The human name (engine.naming): the ``name:`` override, else the
        # generated "<trigger> → <actions>" name; name_source says which.
        **naming.name_fields(workflow),
        "enabled": workflow.get("enabled", True),
        "description": str(workflow.get("description") or ""),
        "source": source,
        "connector": str(primary.get("connector", "?")),
        "event": str(primary.get("event", "?")),
        "triggerCount": len(triggers),
        "actionCount": len(actions or []),
        # Step types for the list's ?q= search and clients.
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


def api_list(q=None, tag=None, folder=None):
    """Managed workflows in the live published store.

    A workflow saved but not yet picked up by the deploy pipeline shows up
    here too — its published state is what actually runs. ``q`` filters
    case-insensitively over each row's id, name, description, trigger connector
    and event, action step types, tags, and folder. ``tag`` narrows to
    workflows carrying exactly that tag (case-insensitive) — Zapier's tag
    view. ``folder`` narrows to workflows sitting in exactly that folder
    (case-insensitive) — Zapier's folder view.

    Every caller sees every workflow; the write-side gate is
    visibility.ensure_can_write, applied by the route layers.
    """
    summaries = {}
    drafts = {}
    if published_workflows.configured():
        for item in published_workflows.load_items(include_drafts=True):
            if item.get("draft_of"):
                drafts[str(item["draft_of"])] = item
                continue
            workflow = item.get("workflow")
            if not isinstance(workflow, dict) or not workflow.get("id"):
                continue
            summary = _summary(workflow, item.get("file") or f"{workflow['id']}.yaml")
            # Informational owner stamp; nothing filters by it.
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
            if workflow_id in summaries:
                summaries[workflow_id]["has_draft"] = True
                continue
            summary = _summary(workflow, item.get("file") or f"{workflow_id}.yaml")
            # Draft-only rows are owned by their drafted_by (visibility.
            # owner_of_item) — exposed informationally like the live rows.
            summaries[str(workflow["id"])] = {**summary, "published": False,
                                              "owner": visibility.owner_of_item(item)}
    ordered = sorted(summaries.values(), key=lambda summary: summary["id"])
    search = str(q or "").strip().lower()
    if search:
        ordered = [
            summary for summary in ordered
            if search in " ".join(
                [summary["id"], summary.get("name") or "",
                 summary.get("description") or "",
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
    byte-identical. Aliases are expanded (multi-trigger workflows share one
    filters mapping), so canonical text stays hand-editable."""
    return yaml.dump(ordered_workflow(workflow), sort_keys=False,
                     Dumper=_NoAliasDumper)


class _NoAliasDumper(yaml.SafeDumper):
    def ignore_aliases(self, data):
        return True


def api_get(source):
    """One workflow from the live published store.

    The payload carries ``yaml``, the canonical text rendered from the stored
    definition, so `workflows export` and the console's download round-trip
    through `workflows save` without re-rendering client-side. Draft state is
    not served here (this is the live read — the toggle/tags/folder/delete
    verbs resolve through it); the list rows carry ``has_draft`` /
    ``published: false`` and the versions list carries the ``draft`` block.
    """
    item = _published_by_file(source)
    if item and isinstance(item.get("workflow"), dict):
        from ...engine import naming

        return 200, {"workflow": item["workflow"], "published": True,
                     **naming.name_fields(item["workflow"]),
                     "yaml": workflow_yaml_text(item["workflow"])}
    return 404, {"error": f"no such workflow: {source}"}

