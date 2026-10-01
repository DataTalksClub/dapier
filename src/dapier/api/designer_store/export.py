"""Export: the single-workflow bundle and the all-workflows zip."""
from datetime import datetime, timezone

import base64
import io
import json
import zipfile

import yaml

from ...auth import visibility
from ...triggers import published_workflows
from .github import sync_status
from .validation import (FILE_PATTERN, WorkflowError, ordered_workflow,
                         parse_workflow)
from .listing import _folder_of, _read_denied, _tags_of, workflow_yaml_text

MAX_EXPORT_WORKFLOWS = 500


def api_export(tag=None, folder=None, now=None, visible=None):
    """Every workflow's canonical YAML as one zip bundle: ``(status, payload)``.

    The one-shot bundle behind `workflows export --all` and the console's
    Export-all button: one ``workflows/<file>.yaml`` entry per workflow named
    by its source file — the same canonical bytes api_get renders, so every
    entry re-saves through `workflows save` byte-identical — plus a
    ``manifest.json`` listing each workflow's file name, enabled state, tags,
    folder, and latest published version (0 when never published). Bundle and
    published store merge exactly like api_list, with the published state
    winning by id, so a save the deploy pipeline has not picked up yet
    exports too. ``tag``/``folder`` narrow the bundle exactly like the list
    (case-insensitive, the same semantics); the default is everything.

    The zip is built in memory (io.BytesIO + zipfile — no /tmp writes, this
    runs in Lambda) with fixed entry timestamps, so the same set of workflows
    always bundles to the same bytes; ``now`` (epoch seconds) only stamps the
    suggested filename (``dapier-workflows-YYYYMMDD.zip``) and the manifest's
    ``exported_at``. Workflows without a usable source file are skipped and
    reported (``skipped``) rather than failing the bundle, and deployments
    over MAX_EXPORT_WORKFLOWS workflows are refused to keep responses sane.
    The API has no binary channel, so the archive travels base64 in the JSON
    payload (``b64``) and the callers decode it; the routes carry the
    ``content-disposition`` attachment header with the dated filename.
    ``visible`` (G17 auth.visibility, None = unrestricted) leaves out the
    workflows the caller may not see, exactly like the list does.
    """
    managed = {}
    versions = {}
    if published_workflows.configured():
        items = published_workflows.load_items()
        if visible is not None:
            items = [item for item in items
                     if visible.owner_visible(visibility.owner_of_item(item))]
        for item in items:
            workflow = item.get("workflow")
            if not isinstance(workflow, dict) or not workflow.get("id"):
                continue
            managed[workflow["id"]] = (workflow, item.get("file"))
            versions[str(workflow["id"])] = int(item.get("revision") or 0)
    if len(managed) > MAX_EXPORT_WORKFLOWS:
        return 400, {"error": f"Too many workflows to export ({len(managed)}); "
                              f"the cap is {MAX_EXPORT_WORKFLOWS}."}
    wanted_tag = str(tag or "").strip().lower()
    wanted_folder = str(folder or "").strip().lower()
    skipped, entries, listed = [], {}, []
    for workflow, source in sorted(managed.values(),
                                   key=lambda pair: str(pair[0].get("id") or "")):
        name = source.strip() if isinstance(source, str) else ""
        if not FILE_PATTERN.fullmatch(name):
            skipped.append(str(workflow.get("id") or "unknown"))
            continue
        tags = _tags_of(workflow)
        workflow_folder = _folder_of(workflow)
        if wanted_tag and wanted_tag not in {existing.lower() for existing in tags}:
            continue
        if wanted_folder and workflow_folder.strip().lower() != wanted_folder:
            continue
        try:
            entries[f"workflows/{name}"] = workflow_yaml_text(workflow)
        except (yaml.YAMLError, ValueError):
            skipped.append(str(workflow.get("id") or name))
            continue
        listed.append({"file": name,
                       "enabled": bool(workflow.get("enabled", True)),
                       "tags": tags,
                       "folder": workflow_folder,
                       "version": versions.get(str(workflow.get("id") or name), 0)})
    stamp = (datetime.now(timezone.utc) if now is None
             else datetime.fromtimestamp(int(now), timezone.utc))
    manifest = {
        "exported_at": stamp.isoformat(),
        "count": len(entries),
        "skipped": sorted(skipped),
        "workflows": sorted(listed, key=lambda row: row["file"]),
    }
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as bundle:
        for arcname in ["manifest.json", *sorted(entries)]:
            info = zipfile.ZipInfo(arcname, date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            if arcname == "manifest.json":
                bundle.writestr(info, json.dumps(manifest, indent=2, sort_keys=True) + "\n")
            else:
                bundle.writestr(info, entries[arcname])
    return 200, {
        "filename": f"dapier-workflows-{stamp:%Y%m%d}.zip",
        "count": len(entries),
        "skipped": sorted(skipped),
        "b64": base64.b64encode(buffer.getvalue()).decode(),
    }






def api_export_all(now=None, visible=None):
    """Every workflow's canonical YAML as one zip: ``(status, payload)``.

    The same canonical bytes api_get renders, one ``workflows/<file>.yaml``
    entry per workflow; bundle and published merge like api_list with the
    published state winning by id, so a save the deploy pipeline has not
    picked up yet exports too. Workflows without a usable source file are
    skipped and reported (``skipped``) rather than failing the bundle. The
    zip ships base64 in the JSON body — the API has no binary channel — and
    the callers decode it (the CLI writes the file, the console downloads
    it). A ``manifest.json`` entry describes the bundle (exported_at ISO,
    count, per-workflow id/source/folder/tags) so the archive is readable
    without unzipping every file; it carries no connections, tokens, or other
    secrets — workflow YAML only. Refuses deployments with more than
    MAX_EXPORT_WORKFLOWS workflows to keep Lambda responses sane. Entries are
    sorted with a fixed timestamp, so the same set of workflows always
    bundles to the same bytes. ``visible`` (G17 auth.visibility, None =
    unrestricted) leaves out the workflows the caller may not see, exactly
    like the list does.
    """
    managed = {}
    if published_workflows.configured():
        items = published_workflows.load_items()
        if visible is not None:
            items = [item for item in items
                     if visible.owner_visible(visibility.owner_of_item(item))]
        for item in items:
            workflow = item.get("workflow")
            if not isinstance(workflow, dict) or not workflow.get("id"):
                continue
            managed[workflow["id"]] = (workflow, item.get("file"))
    if len(managed) > MAX_EXPORT_WORKFLOWS:
        return 400, {"error": f"Too many workflows to export ({len(managed)}); "
                              f"the cap is {MAX_EXPORT_WORKFLOWS}."}
    skipped, entries, listed = [], {}, []
    for workflow, source in managed.values():
        name = source.strip() if isinstance(source, str) else ""
        if not FILE_PATTERN.fullmatch(name):
            skipped.append(str(workflow.get("id") or "unknown"))
            continue
        try:
            entries.setdefault(f"workflows/{name}", workflow_yaml_text(workflow))
        except (yaml.YAMLError, ValueError):
            skipped.append(str(workflow.get("id") or name))
            continue
        listed.append({"id": str(workflow.get("id") or name), "source": name,
                       "folder": _folder_of(workflow), "tags": _tags_of(workflow)})
    stamp = (datetime.now(timezone.utc) if now is None
             else datetime.fromtimestamp(int(now), timezone.utc))
    manifest = {
        "exported_at": stamp.isoformat(),
        "count": len(entries),
        "skipped": sorted(skipped),
        "workflows": sorted(listed, key=lambda row: row["id"]),
    }
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as bundle:
        for arcname in ["manifest.json", *sorted(entries)]:
            info = zipfile.ZipInfo(arcname, date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            if arcname == "manifest.json":
                bundle.writestr(info, json.dumps(manifest, indent=2, sort_keys=True) + "\n")
            else:
                bundle.writestr(info, entries[arcname])
    return 200, {
        "filename": f"dapier-workflows-{stamp:%Y%m%d}.zip",
        "count": len(entries),
        "skipped": sorted(skipped),
        "b64": base64.b64encode(buffer.getvalue()).decode(),
    }


