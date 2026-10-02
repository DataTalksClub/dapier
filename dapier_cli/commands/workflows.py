"""Implementations of the `dapier workflows` commands."""

import base64, json, os, sys
from urllib.parse import quote

from .. import api

__all__ = ["workflows_bulk_enabled", "workflows_delete", "workflows_diff", "workflows_discard", "workflows_draft_diff", "workflows_duplicate", "workflows_export", "workflows_export_all", "workflows_export_all_bundle", "workflows_folder", "workflows_list", "workflows_publish", "workflows_rollback", "workflows_save", "workflows_set_enabled", "workflows_show", "workflows_tags", "workflows_test", "workflows_test_step", "workflows_versions"]


def workflows_list(api_url, debug=False, search=None, tag=None, folder=None):
    params = []
    if search:
        params.append(f"q={quote(search, safe='')}")
    if tag:
        params.append(f"tag={quote(tag, safe='')}")
    if folder:
        params.append(f"folder={quote(folder, safe='')}")
    query = f"?{'&'.join(params)}" if params else ""
    data = api.call(api_url, "GET", f"/api/agent/designer/workflows{query}", debug=debug)
    items = data.get("workflows", [])
    if not items:
        print("No workflows match this search." if search or tag or folder else
              "No workflows yet. Create one in the console or run `dapier workflows save`.")
    for item in items:
        state = "On" if item.get("enabled", True) else "Off"
        if not item.get("published", True):
            # Draft-only: saved in the designer but never published — it
            # fires nothing until `workflows publish` promotes it.
            state = "draft (never published)"
        elif item.get("has_draft"):
            state += " [draft]"
        if item.get("auto_paused"):
            # The engine paused it after consecutive failed runs; `workflows on`
            # is the resume verb (the enable toggle clears the pause).
            state += " (auto-paused)"
        trigger = f"{item.get('connector', '?')}.{item.get('event', '?')}"
        extra = (item.get("triggerCount") or 1) - 1
        if extra > 0:
            trigger = f"{trigger} +{extra}"
        tags = item.get("tags") or []
        item_folder = item.get("folder") or ""
        print(f"{item.get('source') or item['id']:36} {trigger:34} "
              f"{item.get('actionCount', 0)} action(s) {state}"
              + (f" [folder: {item_folder}]" if item_folder else "")
              + (f" [{' '.join(tags)}]" if tags else ""))
    sync = data.get("git_sync") or {}
    target = f"{sync.get('repo', '?')} ({sync.get('branch', '?')} branch)"
    if sync.get("configured"):
        print(f"Workflow saves create drafts; publishing makes them live and syncs to {target}.")
    else:
        print("Workflow saves create drafts; publishing makes them live. Git sync is not configured.")
    return 0


def workflows_show(api_url, file, debug=False):
    data = api.call(api_url, "GET", f"/api/agent/designer/workflows/{file}", debug=debug)
    print(json.dumps(data.get("workflow", {}), indent=2))
    return 0


def workflows_export(api_url, file, output=None, debug=False):
    """The workflow's canonical YAML (api_get's `yaml` field) to stdout or -o.

    The round-trip twin of `workflows save`: exporting a saved workflow and
    saving the export back reproduces the stored definition byte for byte.
    """
    data = api.call(api_url, "GET", f"/api/agent/designer/workflows/{file}", debug=debug)
    yaml_text = data.get("yaml")
    if not yaml_text:
        print(f"The API returned no YAML for {file}.")
        return 5
    if output:
        try:
            with open(output, "w", encoding="utf-8") as handle:
                handle.write(yaml_text)
        except OSError as exc:
            print(f"Cannot write {output}: {exc}")
            return 2
        print(f"Exported {file} to {output}.")
    else:
        print(yaml_text)
    return 0


def workflows_export_all(api_url, output=None, debug=False):
    """`workflows export --all`: the server-built zip of every workflow.

    Thin client over the operator-gated bundle endpoint — the same domain
    function the console's Export-all button drives. The zip (one canonical
    YAML per workflow under its source file name, plus a manifest.json
    listing file, enabled state, tags, folder, and latest version) is built
    server-side, arrives base64 in the JSON body, and is only decoded and
    written here; nothing is fetched per workflow or assembled client-side.
    """
    data = api.call(api_url, "GET", "/api/agent/designer/export", debug=debug)
    count = int(data.get("count") or 0)
    if not count:
        print("No workflows to export. Create one in the console or run `dapier workflows save`.")
        return 2
    try:
        raw = base64.b64decode(data.get("b64") or "")
    except (ValueError, TypeError):
        print("The API returned an unreadable bundle.")
        return 5
    # The server suggests the filename; basename keeps a hostile suggestion
    # from writing outside the caller's directory.
    target = output or os.path.basename(data.get("filename") or "") or "dapier-workflows.zip"
    try:
        with open(target, "wb") as handle:
            handle.write(raw)
    except OSError as exc:
        print(f"Cannot write {target}: {exc}")
        return 2
    skipped = data.get("skipped") or []
    note = f" (skipped: {', '.join(skipped)})" if skipped else ""
    print(f"Exported {count} workflow(s) to {target}{note}.")
    print("Each workflow is workflows/<file>.yaml; manifest.json describes the bundle.")
    return 0


def workflows_export_all_bundle(api_url, out=None, debug=False):
    """`workflows export-all`: the server-built zip of every workflow's YAML.

    Thin client over the operator-gated export-all endpoint — the same domain
    function the console's Export-all button drives. The zip arrives base64
    in the JSON body and is decoded here; unlike `workflows export --all`
    nothing is fetched per workflow or assembled client-side.
    """
    data = api.call(api_url, "GET", "/api/agent/designer/workflows/export-all", debug=debug)
    try:
        raw = base64.b64decode(data.get("b64") or "")
    except (ValueError, TypeError):
        print("The API returned an unreadable bundle.")
        return 5
    # The server suggests the filename; basename keeps a hostile suggestion
    # from writing outside the caller's directory.
    target = out or os.path.basename(data.get("filename") or "") or "dapier-workflows.zip"
    try:
        with open(target, "wb") as handle:
            handle.write(raw)
    except OSError as exc:
        print(f"Cannot write {target}: {exc}")
        return 2
    skipped = data.get("skipped") or []
    note = f" (skipped: {', '.join(skipped)})" if skipped else ""
    print(f"Exported {data.get('count', 0)} workflow(s) to {target}{note}")
    return 0


def workflows_save(api_url, path, rename_from, debug=False):
    try:
        with (sys.stdin if path == "-" else open(path, encoding="utf-8")) as handle:
            yaml_text = handle.read()
    except OSError as exc:
        print(f"Cannot read {path}: {exc}")
        return 2
    return _save_workflow_yaml(api_url, yaml_text, rename_from, debug=debug)


def _save_workflow_yaml(api_url, yaml_text, rename_from, debug=False):
    """The `workflows save` wire call.

    A save writes a draft (nothing goes live); the API answers published:
    false with a draft block. Only a historical live-publish response
    (published: true) prints the live message."""
    body = {"yaml": yaml_text}
    if rename_from:
        body["renameFrom"] = rename_from
    data = api.call(api_url, "PUT", "/api/agent/designer/workflows", body, debug=debug)
    if data.get("published"):
        print(f"Published {data.get('file')} live."
              + (f" Synced commit {str(data['commit'])[:7]}." if data.get("commit") else ""))
    else:
        draft = data.get("draft") or {}
        stale = " behind the live revision" if draft.get("stale") else ""
        print(f"Saved {data.get('file')} as a draft{stale} — "
              f"publish it with `dapier workflows publish {data.get('file')}`.")
    if data.get("git_sync_error"):
        print(f"Warning: Git sync failed ({data['git_sync_error']}).")
    for warning in data.get("warnings") or []:
        print(f"Warning: {warning}")
    return 0


def workflows_set_enabled(api_url, files, enabled, debug=False):
    """Turn one or more workflows on/off through the bulk endpoint (one call,
    per-id results; the API applies the usual toggle semantics to each)."""
    if isinstance(files, str):
        files = [files]
    data = api.call(api_url, "POST", "/api/agent/designer/workflows/bulk",
                    {"ids": list(files), "action": "enable" if enabled else "disable"},
                    debug=debug)
    state = "On" if enabled else "Off"
    for result in data.get("results") or []:
        label = result.get("file") or result.get("id") or "?"
        if result.get("ok"):
            print(f"{label} is {state} — live now.")
            if result.get("commit"):
                print(f"Committed {str(result['commit'])[:7]}.")
            for warning in result.get("warnings") or []:
                print(f"Warning: {warning}")
        else:
            print(f"{label}: {result.get('error') or 'failed'}")
    return 0


def workflows_bulk_enabled(api_url, enabled, tag=None, all_workflows=False, debug=False):
    """Bulk enable/disable by scope (`workflows enable|disable --tag x|--all`):
    every workflow carrying a tag, or all of them, through the same bulk
    endpoint the explicit file list uses. Failures print per workflow —
    nothing fails silently."""
    if not tag and not all_workflows:
        print("Nothing to do: pass --tag <tag> or --all (or name workflow files).")
        return 2
    body = {"action": "enable" if enabled else "disable"}
    if tag:
        body["tag"] = tag
    else:
        body["all"] = True
    data = api.call(api_url, "POST", "/api/agent/designer/workflows/bulk", body, debug=debug)
    state = "On" if enabled else "Off"
    print(f"{data.get('ok', 0)} of {data.get('requested', 0)} workflow(s) set {state} "
          f"[{data.get('scope', '?')}].")
    for result in data.get("results") or []:
        if not result.get("ok"):
            print(f"{result.get('id') or '?'}: {result.get('error') or 'failed'}")
    return 0


def workflows_duplicate(api_url, file, name=None, debug=False):
    """Copy a saved workflow under a new id (the server slugifies `--name`,
    default `<id>-copy`) through the same publish path as a save."""
    body = {"name": name} if name else {}
    data = api.call(api_url, "POST", f"/api/agent/designer/workflows/{file}/duplicate",
                    body, debug=debug)
    new_file = data.get("file") or file
    if data.get("published"):
        print(f"Duplicated {file} as {new_file} and published it live.")
    else:
        print(f"Duplicated {file} as {new_file}.")
    return 0


def workflows_versions(api_url, file, debug=False):
    """Version history for one workflow: who published what, when, and why."""
    data = api.call(api_url, "GET", f"/api/agent/designer/workflows/{file}/versions", debug=debug)
    print(f"{data.get('workflow') or file} is at revision {data.get('revision', '?')}.")
    draft = data.get("draft")
    if draft:
        stale = " (stale — publish would refuse it; save a fresh draft)" if draft.get("stale") else ""
        by = f" by {draft['drafted_by']}" if draft.get("drafted_by") else ""
        print(f"draft: based on v{draft.get('base_revision', 0)}{by}{stale}")
    versions = data.get("versions") or []
    if not versions:
        print("No version history yet — versions are recorded from now on.")
        return 0
    for version in versions:
        marker = " (current)" if version.get("current") else ""
        state = "On" if version.get("enabled", True) else "Off"
        print(f"v{version.get('revision')}  {version.get('published_at', '')}  "
              f"{version.get('cause') or 'save':8} {version.get('published_by') or 'unknown'} "
              f"{state}{marker}")
    return 0


def workflows_rollback(api_url, file, revision, debug=False):
    """Restore an old version through the managed workflow save path."""
    body = {} if revision is None else {"revision": int(revision)}
    label = f"v{revision}" if revision is not None else "the previous version"
    data = api.call(api_url, "POST", f"/api/agent/designer/workflows/{file}/rollback",
                    body, debug=debug)
    if data.get("published"):
        print(f"Rolled {file} back to {label} and published it live.")
    else:
        print(f"Rolled {file} back to {label}.")
    return 0


def workflows_publish(api_url, file, debug=False):
    """Promote a workflow's saved draft live (the agent POST .../publish route):
    the promotion runs through the ordinary publish path, so the git commit,
    version record, and YouTube reconcile behave like any publish; a
    draft-only workflow becomes v1. A stale draft (the live definition moved
    past its base revision) is refused 409."""
    data = api.call(api_url, "POST", f"/api/agent/designer/workflows/{file}/publish",
                    {}, debug=debug)
    revision = data.get("revision")
    suffix = f" as v{revision}" if revision else ""
    print(f"Published {data.get('file') or file}{suffix} — live now."
          + (f" Synced commit {str(data['commit'])[:7]}." if data.get("commit") else ""))
    for warning in data.get("warnings") or []:
        print(f"Warning: {warning}")
    if data.get("git_sync_error"):
        print(f"Warning: Git sync failed ({data['git_sync_error']}).")
    return 0


def workflows_discard(api_url, file, assume_yes=False, debug=False):
    """Throw a workflow's saved draft away (the agent DELETE .../draft route);
    the live definition is untouched."""
    if not assume_yes:
        try:
            answer = input(f"Discard the draft of {file}? The live workflow is "
                           "untouched; the drafted edits are lost [y/N]: ")
        except EOFError:
            # No interactive stdin (scripts, CI): never guess on a destroy.
            print("No terminal to confirm on; pass --yes to discard without a prompt.")
            return 2
        if answer.strip().lower() not in ("y", "yes"):
            print("Cancelled.")
            return 1
    data = api.call(api_url, "DELETE", f"/api/agent/designer/workflows/{file}/draft",
                    debug=debug)
    print(f"Discarded the draft of {data.get('file') or file}; live is untouched.")
    return 0


def workflows_draft_diff(api_url, file, debug=False):
    """Unified diff of a workflow's draft against live (the server builds it;
    the raw text goes to stdout so it pipes into `less` or `patch`)."""
    data = api.call(api_url, "GET",
                    f"/api/agent/designer/workflows/{file}/draft/diff", debug=debug)
    if data.get("same"):
        print("The draft is identical to live.")
        return 0
    diff = data.get("diff") or ""
    if diff:
        print(diff, end="" if diff.endswith("\n") else "\n")
    return 0


def workflows_delete(api_url, file, assume_yes=False, debug=False):
    """Delete a workflow on every surface at once (the agent DELETE route):
    the live published item is removed and one atomic git commit takes the
    YAML out of the repo; version history and past runs survive as history.
    Refused while runs of the workflow are parked on a delay, so a resume
    cannot dangle."""
    if not assume_yes:
        try:
            answer = input(f"Delete {file}? It stops immediately and the YAML is "
                           "deleted from the repo; past runs and version history "
                           "remain [y/N]: ")
        except EOFError:
            # No interactive stdin (scripts, CI): never guess on a destroy.
            print("No terminal to confirm on; pass --yes to delete without a prompt.")
            return 2
        if answer.strip().lower() not in ("y", "yes"):
            print("Cancelled.")
            return 1
    data = api.call(api_url, "DELETE", f"/api/agent/designer/workflows/{file}", debug=debug)
    print(f"Deleted {data.get('file') or file}. It is off now and no deploy will bring it back.")
    if data.get("commit"):
        print(f"Committed the removal ({str(data['commit'])[:7]}).")
    for warning in data.get("warnings") or []:
        print(f"Warning: {warning}")
    if data.get("git_sync_error"):
        print(f"Warning: the git commit failed ({data['git_sync_error']}); "
              "the next deploy may restore the file — retry `workflows delete`.")
    return 0


def _split_tags(spec, what):
    """Comma-separated tags as a clean list, or None when the flag is absent."""
    if spec is None:
        return None
    tags = [part.strip() for part in spec.split(",") if part.strip()]
    return tags or None


def workflows_tags(api_url, file, tags_spec=None, clear=False, add=None,
                   remove=None, debug=False):
    """Edit a workflow's tags (the agent PUT .../tags route): Zapier-style
    organization labels the list filters on. `--tags` replaces the whole set,
    `--add`/`--remove` edit it (removals win, case-insensitive), `--clear`
    empties it. The set lives in the workflow YAML, so it travels with saves,
    deploys, and rollbacks."""
    if clear:
        body = {"tags": []}
    elif tags_spec is not None:
        tags = _split_tags(tags_spec, "--tags")
        if not tags:
            print("Nothing to do: pass --tags 'billing,ops', --add/--remove, or --clear.")
            return 2
        body = {"tags": tags}
    elif add is not None or remove is not None:
        body = {}
        adds = _split_tags(add, "--add")
        removes = _split_tags(remove, "--remove")
        if adds:
            body["add"] = adds
        if removes:
            body["remove"] = removes
        if not body:
            print("Nothing to do: pass --tags 'billing,ops', --add/--remove, or --clear.")
            return 2
    else:
        print("Nothing to do: pass --tags 'billing,ops', --add/--remove, or --clear.")
        return 2
    data = api.call(api_url, "PUT", f"/api/agent/designer/workflows/{file}/tags",
                    body, debug=debug)
    shown = ", ".join(data.get("tags") or []) or "(none)"
    print(f"Tags for {data.get('file') or file}: {shown}"
          + (" — published live." if data.get("published") else "."))
    if data.get("git_sync_error"):
        print(f"Warning: the git commit failed ({data['git_sync_error']}); "
              "the tags are live but the next deploy may not carry them.")
    return 0


def workflows_folder(api_url, file, set_value=None, clear=False, debug=False):
    """Put a workflow in a Zapier-style folder (`--set "Name"`) or take it
    out (`--clear`) through the agent PUT .../folder route. Folders are flat —
    at most one per workflow, and a name, never a path — and the folder lives
    in the workflow YAML, so it travels with saves, deploys, and rollbacks."""
    if clear and set_value is not None:
        print("Pass --set <name> or --clear, not both.")
        return 2
    if not clear and set_value is None:
        print("Nothing to do: pass --set 'Name' or --clear.")
        return 2
    body = {"folder": "" if clear else set_value}
    data = api.call(api_url, "PUT", f"/api/agent/designer/workflows/{file}/folder",
                    body, debug=debug)
    shown = data.get("folder") or "(none)"
    print(f"Folder for {data.get('file') or file}: {shown}"
          + (" — published live." if data.get("published") else "."))
    if data.get("git_sync_error"):
        print(f"Warning: the git commit failed ({data['git_sync_error']}); "
              "the folder is live but the next deploy may not carry it.")
    return 0


def _read_workflow_yaml(path):
    """The workflow YAML from a path or stdin; (text, None) or (None, message)."""
    try:
        with (sys.stdin if path == "-" else open(path, encoding="utf-8")) as handle:
            return handle.read(), None
    except OSError as exc:
        return None, f"Cannot read {path}: {exc}"


def _load_json_arg(spec, what):
    """A JSON argument, inline or @file; (value, None) or (None, message)."""
    try:
        if spec.startswith("@"):
            with open(spec[1:], encoding="utf-8") as handle:
                return json.load(handle), None
        return json.loads(spec), None
    except OSError as exc:
        return None, f"Cannot read {spec[1:]}: {exc}"
    except ValueError as exc:
        return None, f"Invalid {what} JSON: {exc}"


def _print_test_steps(data):
    """The steps list of a test-run (or per-step test) report."""
    for index, step in enumerate(data.get("steps") or [], 1):
        head = f"  {index}. {step.get('action_id', '?')} ({step.get('action_type', '?')})"
        if step.get("ok"):
            print(f"{head}: ok")
        else:
            print(f"{head}: {step.get('error') or 'failed'}")
        for warning in step.get("warnings") or []:
            print(f"     warning: {warning}")
        if step.get("rendered_input") is not None:
            print(f"     input: {json.dumps(step['rendered_input'], sort_keys=True)}")
        if step.get("output") is not None:
            print(f"     output: {json.dumps(step['output'], sort_keys=True)}")


def workflows_test(api_url, path, event_spec, execute=False, strict=False, debug=False):
    """Test-run a workflow YAML against a sample event, via the agent API.

    Default is a dry-run: per-step rendered inputs with no side effects.
    --execute really runs the actions; --strict makes dry-run warnings
    (a required field the sample renders empty, a value implausible for its
    declared type) fail the step. Exits 0 when every step is ok and a
    trigger actually matched; 1 when the run found problems (a failed step,
    an unsupported action, or no trigger match), so it can gate scripts.
    """
    yaml_text, error = _read_workflow_yaml(path)
    if error:
        print(error)
        return 2
    sample, error = _load_json_arg(event_spec, "sample event")
    if error:
        print(error)
        return 2
    if not isinstance(sample, dict):
        print("The sample event must be a JSON object.")
        return 2
    data = api.call(api_url, "POST", "/api/agent/designer/workflows/test",
                    {"yaml": yaml_text, "event": sample, "execute": execute,
                     "strict": strict}, debug=debug)
    label = data.get("file") or path
    matched = bool(data.get("matched"))
    enabled = bool(data.get("enabled", True))
    print(f"{'Executed' if data.get('mode') == 'execute' else 'Dry run'}: {label} — "
          f"{'a trigger matches' if matched else 'NO trigger matches'} the sample event"
          + ("" if enabled else " (the workflow is disabled)"))
    _print_test_steps(data)
    if data.get("error"):
        print(f"Run error: {data['error']}")
    if not data.get("ok") or not matched:
        if data.get("ok") and not matched:
            print("Nothing would run: no trigger matches this event.")
        return 1
    return 0


def workflows_test_step(api_url, path, action_id, event_spec, steps_spec=None,
                        execute=False, debug=False):
    """Test one step of a workflow against a sample event, via the agent API.

    Zapier's per-step "Test step": the step's inputs render against the
    sample (plus prior steps' outputs from --steps, shaped like the run
    history, so {steps.<id>.output.*} templates fill in), and --execute
    really runs this one step through the real dispatch. Logic steps are
    evaluated, never executed: the branch a run would take, the wait a delay
    would take, what a loop would iterate. Exits 0 when the step passed,
    1 when it failed, 2 on input errors.
    """
    yaml_text, error = _read_workflow_yaml(path)
    if error:
        print(error)
        return 2
    sample, error = _load_json_arg(event_spec, "sample event")
    if error:
        print(error)
        return 2
    if not isinstance(sample, dict):
        print("The sample event must be a JSON object.")
        return 2
    steps_context = None
    if steps_spec:
        steps_context, error = _load_json_arg(steps_spec, "steps")
        if error:
            print(error)
            return 2
        if not isinstance(steps_context, dict):
            print("The steps context must be a JSON object shaped like run history "
                  "({action_id: {status, output}}).")
            return 2
    body = {"yaml": yaml_text, "action_id": action_id, "event": sample,
            "execute": execute}
    if steps_context:
        body["steps"] = steps_context
    data = api.call(api_url, "POST", "/api/agent/designer/workflows/test-step",
                    body, debug=debug)
    label = data.get("file") or path
    print(f"{'Executed' if execute else 'Tested'} step "
          f"{data.get('action_id') or action_id} of {label} — "
          f"{'ok' if data.get('ok') else 'FAILED'}")
    _print_test_steps(data)
    if data.get("error"):
        print(f"Run error: {data['error']}")
    return 0 if data.get("ok") else 1




def workflows_diff(api_url, file, from_revision, to_revision, debug=False):
    """Unified diff between two published versions (the server builds it;
    the raw text goes to stdout so it pipes into `less` or `patch`)."""
    data = api.call(api_url, "GET",
                    f"/api/agent/designer/workflows/{file}/versions/diff"
                    f"?from={from_revision}&to={to_revision}", debug=debug)
    diff = data.get("diff") or ""
    if diff:
        sys.stdout.write(diff if diff.endswith("\n") else diff + "\n")
    if data.get("same"):
        print(f"v{from_revision} and v{to_revision} are identical.")
    if data.get("truncated"):
        print("Warning: the diff was truncated at the server's size cap; "
              "narrow the revision range to see more.", file=sys.stderr)
    return 0

