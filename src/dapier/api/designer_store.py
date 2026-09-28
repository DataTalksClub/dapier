"""Designer save and test-run paths: validate workflow YAML, commit it to
GitHub, publish it live, and dry-run it against a sample event.

The console designer at /designer edits workflows/<id>.yaml in the dapier
repo. Saves are validated server-side (structure only — action types beyond
the catalog are legal and preserved verbatim) and committed with the GitHub
git data API, so one save can create, update, and rename in a single commit
on the configured branch. The commit is the version record; a successful save
then publishes the parsed definition to PUBLISHED_WORKFLOWS_TABLE, which the
engine merges over the deployed bundle — so a save runs on the next event
without waiting for the deploy pipeline. The enable/disable toggle flips the
published item the same way (live immediately) and commits the flipped YAML
best-effort so the next deploy agrees with the live state.
"""

import base64
import json
import os
import re
import urllib.error
import urllib.request
from pathlib import Path

import yaml

from ..triggers import published_workflows

DEFAULT_REPO_URL = "https://github.com/DataTalksClub/dapier"
REPO_URL_ENV = "WORKFLOWS_REPO_URL"
BRANCH_ENV = "WORKFLOWS_GITHUB_BRANCH"
TOKEN_SECRET_ENV = "GITHUB_WORKFLOWS_TOKEN_SECRET"
GITHUB_API = "https://api.github.com"
MAX_YAML_BYTES = 100_000

FILE_PATTERN = re.compile(r"^[a-z0-9][a-z0-9._-]*\.yaml$", re.IGNORECASE)
ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_-]{0,62}$")

# Logic-step bounds the engine enforces at runtime; rejected here at save time.
# A delay past the engine's 60s inline sleep cap suspends the run and resumes
# it from the queue, so totals up to DELAY_MAX_DAYS are legal (the executions
# table's 90-day TTL is the ceiling — see engine.logic.MAX_SUSPENDED_SECONDS).
DELAY_MAX_DAYS = 90
LOOP_MAX_ITERATIONS = 100
# Mirrors engine.matching._matches_filter (and the designer's logicOperators):
# the flat field/operator/value shorthand accepts any operator the engine's
# ``when`` mappings do.
FILTER_OPERATORS = (
    "equals", "not_equals", "in", "prefix", "suffix", "contains",
    "does_not_contain", "gt", "gte", "lt", "lte", "exists", "empty",
)
DELAY_TEMPLATE = re.compile(r"^\{[^{}]+\}$")


class WorkflowError(ValueError):
    """Invalid workflow definition."""


class SyncConfigError(Exception):
    """Git sync is not configured (no token secret wired to the stack)."""


class SyncError(Exception):
    """GitHub refused or failed the commit."""


def repo_slug():
    url = os.environ.get(REPO_URL_ENV, DEFAULT_REPO_URL).rstrip("/")
    match = re.search(r"github\.com[/:]([^/]+)/([^/]+?)(?:\.git)?$", url)
    if not match:
        raise SyncConfigError(f"cannot parse owner/repo from {REPO_URL_ENV}")
    return f"{match.group(1)}/{match.group(2)}"


def branch():
    return os.environ.get(BRANCH_ENV, "main").strip() or "main"


def sync_status():
    """What the console needs to show about the save target."""
    configured = bool(os.environ.get(TOKEN_SECRET_ENV))
    return {
        "git_sync": {
            "configured": configured,
            "repo": repo_slug(),
            "branch": branch(),
        },
    }


def _bundle_root():
    # Same default as matching._root and overview._workflows: the repo's
    # workflows/ two levels above src/ (parents[3] of this file) — the parent-
    # parent default used to point at src/dapier/workflows, which never exists,
    # so the designer's workflow list lost every bundled workflow and the
    # console opened the same seeded draft for all of them.
    return Path(os.environ.get("WORKFLOWS_DIR", Path(__file__).resolve().parents[3] / "workflows"))


def _bundled_workflows():
    """(workflow, filename) pairs this deployment's bundle carries (deploy-time git state)."""
    from ..engine import matching

    result = []
    for path in sorted(_bundle_root().glob("*.yaml")):
        try:
            workflow = yaml.safe_load(path.read_text())
        except yaml.YAMLError:
            continue
        if isinstance(workflow, dict) and "id" in workflow and matching.workflow_triggers(workflow):
            result.append((workflow, path.name))
    return result


def _summary(workflow, source):
    """One list row: primary trigger, how many there are, and the effective actions."""
    from ..engine import matching

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
    }


def bundled_summaries():
    """Summaries of the bundled workflows (deploy-time git state)."""
    return [_summary(workflow, source) for workflow, source in _bundled_workflows()]


def bundled_yaml(source):
    """Raw parsed YAML of one bundled workflow, or None if not deployed."""
    if not FILE_PATTERN.fullmatch(source or ""):
        return None
    path = _bundle_root() / source
    if not path.exists():
        return None
    try:
        return yaml.safe_load(path.read_text())
    except yaml.YAMLError:
        return None


def _published_by_file(source):
    """The published item for one file name, or None (invalid name / not published)."""
    if not published_workflows.configured() or not FILE_PATTERN.fullmatch(source or ""):
        return None
    return published_workflows.get_item(source.removesuffix(".yaml"))


def api_list(q=None):
    """Bundled workflows with the live published state overlaid by id.

    A workflow saved but not yet picked up by the deploy pipeline shows up
    here too — its published state is what actually runs. ``q`` filters
    case-insensitively over each row's id, description, trigger connector
    and event, and action step types.
    """
    summaries = {
        summary["id"]: {**summary, "published": False}
        for summary in bundled_summaries()
    }
    if published_workflows.configured():
        for item in published_workflows.load_items():
            workflow = item.get("workflow")
            if not isinstance(workflow, dict) or not workflow.get("id"):
                continue
            summary = _summary(workflow, item.get("file") or f"{workflow['id']}.yaml")
            if summary["id"] not in summaries:
                summary["deployed"] = False
            summaries[summary["id"]] = {**summary, "published": True}
    ordered = sorted(summaries.values(), key=lambda summary: summary["id"])
    search = str(q or "").strip().lower()
    if search:
        ordered = [
            summary for summary in ordered
            if search in " ".join(
                [summary["id"], summary.get("description") or "",
                 summary.get("connector") or "", summary.get("event") or "",
                 *(summary.get("actionTypes") or [])]).lower()
        ]
    return 200, {"workflows": ordered, **sync_status()}


def workflow_yaml_text(workflow):
    """The canonical YAML text for a stored definition — the same dump the
    save/toggle/duplicate/rollback paths write, so an export re-saves
    byte-identical."""
    return yaml.safe_dump(ordered_workflow(workflow), sort_keys=False)


def api_get(source):
    """One workflow: the live published state first, then the bundle, then
    committed git state (covering a save whose deploy has not finished).

    The payload carries ``yaml``, the canonical text rendered from the stored
    definition, so `workflows export` and the console's download round-trip
    through `workflows save` without re-rendering client-side.
    """
    item = _published_by_file(source)
    if item and isinstance(item.get("workflow"), dict):
        return 200, {"workflow": item["workflow"], "published": True,
                     "yaml": workflow_yaml_text(item["workflow"])}
    workflow = bundled_yaml(source)
    if workflow is None:
        try:
            workflow = fetch_workflow(source)
        except KeyError:
            return 404, {"error": f"no such workflow: {source}"}
        except SyncConfigError:
            # Nowhere else it could live: not deployed and no git to look in.
            return 404, {"error": f"no such workflow: {source}"}
        except (SyncError, WorkflowError) as exc:
            return 502, {"error": f"git fetch failed: {exc}"}
    return 200, {"workflow": workflow, "published": False,
                 "yaml": workflow_yaml_text(workflow)}


def api_save(body, operator=None, cause="save", message=None):
    """Validate and commit a workflow definition, then publish it live.

    Returns (status, payload). ``published`` in the payload says whether the
    definition is already running; a publish failure after a successful commit
    is a loud 502 (the commit sha is included), and retrying the save is safe.
    ``cause`` labels the version record (save or rollback); ``message``
    overrides the git commit subject.
    """
    if not isinstance(body, dict):
        return 400, {"error": "request body must be an object"}
    yaml_text = body.get("yaml")
    rename_from = body.get("renameFrom")
    if rename_from is not None and not isinstance(rename_from, str):
        return 400, {"error": "renameFrom must be a file name"}
    try:
        workflow = parse_workflow(yaml_text)
        result = commit_workflow(
            yaml_text,
            rename_from=rename_from,
            message=message or f"designer: save workflow {workflow['id']}",
        )
    except WorkflowError as exc:
        return 400, {"error": str(exc)}
    except SyncConfigError as exc:
        return 503, {"error": str(exc)}
    except SyncError as exc:
        return 502, {"error": str(exc)}
    return _publish_committed(workflow, result, rename_from=rename_from, operator=operator, cause=cause)


def _publish_committed(workflow, result, *, rename_from=None, operator=None, cause="save"):
    """Publish the just-committed definition; also drop a renamed-away id."""
    if not published_workflows.configured():
        return 200, {**result, "published": False}
    try:
        previous = published_workflows.get_item(workflow["id"])
        published_workflows.publish(workflow, operator=operator, previous=previous, cause=cause)
        if rename_from and rename_from != result["file"]:
            published_workflows.unpublish(rename_from.removesuffix(".yaml"))
    except Exception as exc:
        return 502, {"error": f"committed to git but not live: {exc}",
                     "file": result["file"], "commit": result["commit"], "published": False}
    return 200, {**result, "published": True}


def api_toggle(source, body, operator=None):
    """Flip a workflow's enabled flag live, then commit the flipped YAML.

    The published store is updated first — the toggle runs on the next event —
    and the git commit follows best-effort so the next deploy agrees. A git
    failure is reported in the payload but does not undo the live toggle.
    """
    if not isinstance(body, dict) or not isinstance(body.get("enabled"), bool):
        return 400, {"error": 'body must be {"enabled": true|false}'}
    if not published_workflows.configured():
        return 503, {"error": "published workflows are not configured"}
    if not FILE_PATTERN.fullmatch(source or ""):
        return 400, {"error": f"invalid workflow file name: {source!r}"}
    status, payload = api_get(source)
    if status != 200:
        return status, payload
    workflow = {**payload["workflow"], "enabled": body["enabled"]}
    try:
        previous = published_workflows.get_item(workflow["id"])
        published_workflows.publish(workflow, operator=operator, previous=previous, cause="toggle")
    except Exception as exc:
        return 502, {"error": f"publish failed: {exc}"}
    result = {
        "file": source,
        "enabled": body["enabled"],
        "published": True,
    }
    try:
        committed = commit_workflow(
            workflow_yaml_text(workflow),
            message=f"designer: {'enable' if body['enabled'] else 'disable'} workflow {workflow['id']}",
        )
        result["commit"] = committed["commit"]
    except (SyncConfigError, SyncError) as exc:
        result["git_sync_error"] = str(exc)
    return 200, result


def api_versions(source):
    """Version history for one workflow, newest revision first.

    Every save, toggle, and rollback publishes a version record; this lists
    them with who published each one, when, and why, and flags the live
    revision as ``current``.
    """
    if not published_workflows.configured():
        return 503, {"error": "published workflows are not configured"}
    if not FILE_PATTERN.fullmatch(source or ""):
        return 400, {"error": f"invalid workflow file name: {source!r}"}
    status, payload = api_get(source)
    if status != 200:
        return status, payload
    workflow = payload["workflow"]
    live = published_workflows.get_item(workflow["id"]) or {}
    live_revision = int(live.get("revision") or 0)
    versions = [{
        "revision": int(version.get("revision") or 0),
        "published_by": version.get("published_by") or "",
        "published_at": version.get("published_at") or "",
        "cause": version.get("cause") or "save",
        "enabled": bool(version.get("enabled", True)),
        "current": int(version.get("revision") or 0) == live_revision,
    } for version in published_workflows.list_versions(workflow["id"])]
    return 200, {
        "workflow": workflow["id"],
        "file": source,
        "revision": live_revision,
        "versions": versions,
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
    return api_save(
        {"yaml": yaml_text},
        operator=operator,
        cause="rollback",
        message=f"designer: rollback workflow {workflow['id']} to v{revision}",
    )


# Per-run bookkeeping a stored workflow could carry; a copy starts fresh.
# Everything else — including action step ids — is part of the definition and
# is copied verbatim.
RUN_STATE_KEYS = ("run_state", "last_run", "last_run_at", "run_count", "stats")


def slugify_id(text):
    """A workflow id from free text: lowercase, runs of characters outside
    the id alphabet (everything but alphanumerics and underscores) collapse
    to one hyphen, and the result fits ID_PATTERN (alnum first, 63 chars
    max). Underscores survive — bundled ids like dropbox_on_upload use them.
    Empty when nothing usable survives."""
    slug = re.sub(r"[^a-z0-9_]+", "-", str(text).strip().lower())
    slug = re.sub(r"-+", "-", slug).strip("-")
    slug = re.sub(r"^[^a-z0-9]+", "", slug)
    return slug[:63].rstrip("-")


def _workflow_exists(workflow_id, filename):
    """Whether a workflow already answers to this id/file: the deployed
    bundle, the live published store, then committed git state (a save whose
    deploy has not finished). A git probe failure cannot be answered, so it
    does not count as existing — the commit that follows reports its own
    errors."""
    if bundled_yaml(filename) is not None:
        return True
    if published_workflows.configured() and published_workflows.get_item(workflow_id):
        return True
    if os.environ.get(TOKEN_SECRET_ENV):
        try:
            fetch_workflow(filename)
            return True
        except KeyError:
            return False
        except (SyncConfigError, SyncError):
            pass
    return False


def api_duplicate(source, body=None, operator=None):
    """Copy one workflow under a new id: load it, rename the id (and so the
    file slug), strip run-state bookkeeping, and save through the same
    commit-and-publish path api_save uses — so publishing and the git commit
    behave identically. The original file, id, and published item are left
    untouched.

    ``body`` optionally carries ``{"name": "..."}``; without it the copy is
    named ``<id>-copy`` (the workflow's id is its name). Returns api_save's
    response shape plus ``duplicated_from``.
    """
    if not FILE_PATTERN.fullmatch(source or ""):
        return 400, {"error": f"invalid workflow file name: {source!r}"}
    if body is None:
        body = {}
    if not isinstance(body, dict):
        return 400, {"error": "request body must be an object"}
    new_name = body.get("name")
    if new_name is not None and (not isinstance(new_name, str) or not new_name.strip()):
        return 400, {"error": "name must be a non-empty string"}
    status, payload = api_get(source)
    if status != 200:
        return status, payload
    workflow = payload["workflow"]
    base_name = new_name.strip() if new_name else f"{workflow['id']}-copy"
    new_id = slugify_id(base_name)
    if not new_id:
        return 400, {"error": f"cannot derive a workflow id from {base_name!r}"}
    if new_id == str(workflow["id"]) or _workflow_exists(new_id, filename_for(new_id)):
        return 409, {"error": f"a workflow named '{new_id}' already exists"}
    copy = {key: value for key, value in workflow.items() if key not in RUN_STATE_KEYS}
    copy["id"] = new_id
    yaml_text = workflow_yaml_text(copy)
    try:
        result = commit_workflow(
            yaml_text,
            message=f"designer: duplicate workflow {workflow['id']} as {new_id}",
        )
    except WorkflowError as exc:
        return 400, {"error": str(exc)}
    except SyncConfigError as exc:
        return 503, {"error": str(exc)}
    except SyncError as exc:
        return 502, {"error": str(exc)}
    status, payload = _publish_committed(copy, result, operator=operator)
    payload["duplicated_from"] = source
    return status, payload


def filename_for(workflow_id):
    return f"{workflow_id}.yaml"


def _workflow_under_test(source, body):
    """The workflow a test targets: ``(status, payload)``.

    The inline draft definition in the body (``workflow`` object or ``yaml``
    text — the point of the feature is testing what is about to be saved),
    else the current file (published overlay / bundled YAML / committed git
    state). Success carries ``{"workflow": ..., "label": ...}``; anything
    else is ``{"error": ...}``.
    """
    inline = body.get("workflow")
    yaml_text = body.get("yaml")
    if inline is not None and not isinstance(inline, dict):
        return 400, {"error": "workflow must be an object"}
    if inline is None and yaml_text is not None and not isinstance(yaml_text, str):
        return 400, {"error": "yaml must be a string"}
    try:
        if inline is not None:
            # Round-trip through the same validation the save path applies.
            workflow = parse_workflow(yaml.safe_dump(inline))
            return 200, {"workflow": workflow, "label": filename_for(str(workflow["id"]))}
        if isinstance(yaml_text, str):
            workflow = parse_workflow(yaml_text)
            return 200, {"workflow": workflow, "label": filename_for(str(workflow["id"]))}
        if not source:
            return 400, {"error": "pass the workflow inline (workflow or yaml) "
                                  "or test a saved file: /workflows/{file}/test"}
        status, payload = api_get(source)
        if status != 200:
            return status, payload
        return 200, {"workflow": payload["workflow"], "label": source}
    except WorkflowError as exc:
        return 400, {"error": str(exc)}


def api_test_run(source, body, operator=None):
    """Test-run one workflow against a sample event: dry-run by default,
    real execution with ``execute: true``.

    The workflow under test is the inline draft (see _workflow_under_test) or
    the saved file. ``event`` is the sample event payload. Read-only unless
    ``execute`` is set. ``strict: true`` fails dry-run steps whose rendered
    inputs trip the registry's field rules (a required field the sample
    renders empty, an implausible typed value) instead of reporting them as
    warnings.
    """
    del operator  # recorded by the calling route; the run itself is read-only
    if not isinstance(body, dict):
        return 400, {"error": "request body must be an object"}
    sample = body.get("event")
    if not isinstance(sample, dict):
        return 400, {"error": 'body must include "event": the sample event object'}
    if not isinstance(body.get("execute", False), bool):
        return 400, {"error": "execute must be a boolean"}
    if not isinstance(body.get("strict", False), bool):
        return 400, {"error": "strict must be a boolean"}
    status, payload = _workflow_under_test(source, body)
    if status != 200:
        return status, payload

    from ..engine import dryrun

    try:
        report = dryrun.test_run(payload["workflow"], sample,
                                 execute=bool(body.get("execute", False)),
                                 strict=bool(body.get("strict", False)))
    except dryrun.TestRunError as exc:
        return 400, {"error": str(exc)}
    report["file"] = payload["label"]
    return 200, report


def api_test_step(source, body, operator=None):
    """Test ONE step of a workflow against a sample event (Zapier's per-step
    "Test step"): the step's inputs render against the sample and, with
    ``execute: true``, the step really runs — side effects limited to this
    one step, nothing recorded to the executions table.

    Same workflow resolution as api_test_run. Body: ``action_id`` (the
    step's run-history id — a top-level action id, or a branch step like
    ``route.invoices.0`` or ``send.error.alert``), ``event`` (the sample),
    optional ``steps`` (prior steps' outputs, shaped like the run history, so
    ``{steps.<id>.output.*}`` templates render against real data), and
    ``execute`` (default false: render and evaluate only — logic steps are
    evaluated either way, never executed).
    """
    del operator  # recorded by the calling route; the step itself is unrecorded
    if not isinstance(body, dict):
        return 400, {"error": "request body must be an object"}
    action_id = body.get("action_id")
    if not isinstance(action_id, str) or not action_id.strip():
        return 400, {"error": 'body must include "action_id": the step to test'}
    sample = body.get("event")
    if not isinstance(sample, dict):
        return 400, {"error": 'body must include "event": the sample event object'}
    steps_context = body.get("steps")
    if steps_context is None:
        steps_context = {}
    if not isinstance(steps_context, dict) or not all(
            isinstance(value, dict) for value in steps_context.values()):
        return 400, {"error": 'steps must be an object of {action_id: {status, output}}'}
    if not isinstance(body.get("execute", False), bool):
        return 400, {"error": "execute must be a boolean"}
    status, payload = _workflow_under_test(source, body)
    if status != 200:
        return status, payload

    from ..engine import dryrun

    try:
        report = dryrun.test_step(payload["workflow"], action_id, sample,
                                  execute=bool(body.get("execute", False)),
                                  step_outputs=steps_context)
    except dryrun.TestRunError as exc:
        return 400, {"error": str(exc)}
    report["file"] = payload["label"]
    return 200, report


# Top-level key order for workflow YAML the server writes; the designer
# client emits the same order. Stored dicts keep whatever order they were
# parsed in — this only applies at dump time.
WORKFLOW_KEY_ORDER = ("id", "enabled", "actions", "flows", "flow", "trigger", "triggers")


def ordered_workflow(workflow):
    """The workflow mapping in canonical key order (actions before trigger),
    with keys outside the canon kept at the end in their original order."""
    ordered = {key: workflow[key] for key in WORKFLOW_KEY_ORDER if key in workflow}
    ordered.update(
        (key, value) for key, value in workflow.items() if key not in ordered
    )
    return ordered


def parse_workflow(yaml_text):
    """Structural validation mirroring what engine.py reads at runtime.

    Deliberately permissive on action internals: types outside the catalog
    round-trip untouched, mirroring the designer client.
    """
    if not isinstance(yaml_text, str) or not yaml_text.strip():
        raise WorkflowError("workflow YAML is required")
    if len(yaml_text.encode()) > MAX_YAML_BYTES:
        raise WorkflowError("workflow YAML is too large")
    try:
        workflow = yaml.safe_load(yaml_text)
    except yaml.YAMLError as exc:
        raise WorkflowError(f"invalid YAML: {exc}") from exc
    if not isinstance(workflow, dict):
        raise WorkflowError("workflow must be a YAML object")

    workflow_id = workflow.get("id")
    if not isinstance(workflow_id, str) or not ID_PATTERN.fullmatch(workflow_id.strip()):
        raise WorkflowError("workflow needs an id: letters, digits, hyphens or underscores (max 63 chars)")
    workflow_id = workflow_id.strip()

    triggers = workflow.get("triggers")
    trigger = workflow.get("trigger")
    if isinstance(triggers, list) and triggers:
        for entry in triggers:
            if not isinstance(entry, dict) or not str(entry.get("connector") or "").strip() \
                    or not str(entry.get("event") or "").strip():
                raise WorkflowError("every trigger needs a connector and an event")
    elif isinstance(trigger, dict):
        if not str(trigger.get("connector") or "").strip() or not str(trigger.get("event") or "").strip():
            raise WorkflowError("trigger needs a connector and an event")
    else:
        raise WorkflowError("workflow needs a trigger (or a triggers list)")

    flow = str(workflow.get("flow") or "").strip()
    actions = workflow.get("actions")
    if flow and actions:
        raise WorkflowError("bind either inline actions or a flow, not both")
    if flow:
        from ..engine import matching

        if matching.flow_actions(flow) is None:
            raise WorkflowError(f"no shared flow named '{flow}'")
    elif not isinstance(actions, list) or not actions:
        raise WorkflowError("connect at least one action to the trigger")
    for action in (actions or []):
        if not isinstance(action, dict) or not str(action.get("type") or "").strip():
            raise WorkflowError("every action needs a type")
        if action.get("type") == "code" and not str(action.get("code") or "").strip():
            raise WorkflowError("every code action needs non-empty code")
        from ..engine.actions import templating

        try:
            templating.validate_action(action)
        except templating.TemplateError as exc:
            raise WorkflowError(f"action '{action.get('type')}': {exc}") from exc
    _validate_steps(actions)
    return workflow


def _validate_steps(steps, where="actions"):
    """Structural validation of a step chain, recursing into logic steps.

    Connector action internals stay permissive (the engine owns them); the
    logic step kinds — filter, condition, paths, delay, for_each, digest — are checked here
    so a bad delay bound or an empty loop body fails the save with a clear
    error instead of failing at run time. The generic on_error/error_actions
    keys are shape-checked on every step, and error_actions recurses as a
    nested chain.
    """
    from ..engine import logic
    from ..connectors import registry

    for index, step in enumerate(steps or []):
        if not isinstance(step, dict) or not str(step.get("type") or "").strip():
            raise WorkflowError(f"{where}[{index}]: every step needs a type")
        kind = str(step["type"])
        label = str(step.get("id") or f"{where}[{index}]")
        if kind in ("filter", "condition"):
            when = step.get("when")
            if when is not None and not isinstance(when, dict):
                raise WorkflowError(f"step '{label}': when must be a mapping of field rules")
            if logic.predicate_rules(step) is None:
                raise WorkflowError(f"step '{label}': {kind} needs a when mapping or a field")
            operator = str(step.get("operator") or "").strip()
            if operator and operator not in FILTER_OPERATORS:
                raise WorkflowError(
                    f"step '{label}': operator must be one of {', '.join(FILTER_OPERATORS)}")
        if kind == "condition":
            for branch in ("then", "else"):
                value = step.get(branch)
                if value is None:
                    continue
                if not isinstance(value, list):
                    raise WorkflowError(f"step '{label}': condition {branch} must be a list of steps")
                _validate_steps(value, where=f"{label}.{branch}")
        if kind == "paths":
            branches = step.get("paths")
            if not isinstance(branches, list) or not branches:
                raise WorkflowError(f"step '{label}': paths needs a non-empty list of paths")
            for position, branch in enumerate(branches):
                branch_label = (str(branch.get("label") or f"paths[{position}]")
                                if isinstance(branch, dict) else f"paths[{position}]")
                if not isinstance(branch, dict):
                    raise WorkflowError(
                        f"step '{label}': path '{branch_label}' must be a mapping")
                if logic.predicate_rules(branch) is None:
                    raise WorkflowError(
                        f"step '{label}': path '{branch_label}' needs a when mapping or a field")
                operator = str(branch.get("operator") or "").strip()
                if operator and operator not in FILTER_OPERATORS:
                    raise WorkflowError(
                        f"step '{label}': path '{branch_label}' operator must be one of "
                        f"{', '.join(FILTER_OPERATORS)}")
                actions = branch.get("actions")
                if actions is None:
                    continue
                if not isinstance(actions, list):
                    raise WorkflowError(
                        f"step '{label}': path '{branch_label}' actions must be a list of steps")
                _validate_steps(actions, where=f"{label}.{branch_label}")
            default_steps = step.get("default")
            if default_steps is not None:
                if not isinstance(default_steps, list):
                    raise WorkflowError(f"step '{label}': paths default must be a list of steps")
                _validate_steps(default_steps, where=f"{label}.default")
        if kind == "delay":
            _validate_delay(step, label)
        if kind == "digest":
            _validate_digest(step, label)
        if kind == "for_each":
            if not str(step.get("list") or "").strip().strip("{}").strip():
                raise WorkflowError(f"step '{label}': for_each needs a list field")
            item = str(step.get("item") or "item").strip() or "item"
            if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", item):
                raise WorkflowError(f"step '{label}': for_each item must be a template variable name")
            body = step.get("actions")
            if not isinstance(body, list) or not body:
                raise WorkflowError(f"step '{label}': for_each needs at least one step in actions")
            _validate_steps(body, where=label)
            iterations = step.get("max_iterations")
            if iterations is not None and (
                    isinstance(iterations, bool) or not isinstance(iterations, int)
                    or iterations < 1 or iterations > LOOP_MAX_ITERATIONS):
                raise WorkflowError(
                    f"step '{label}': for_each max_iterations must be between 1 and "
                    f"{LOOP_MAX_ITERATIONS}")
        registry.validate_error_keys(step, f"step '{label}'", WorkflowError)
        registry.validate_on_fail_key(step, f"step '{label}'", WorkflowError)
        registry.validate_autoretry_key(step, f"step '{label}'", WorkflowError)
        # The same typed-field rules stored trigger chains answer to (see
        # validate_action_chain): a literal clearly wrong for its declared
        # field type fails the save. Templated values are skipped there; the
        # dry-run re-checks them against a sample event.
        registry.validate_field_types(step, f"step '{label}'", WorkflowError)
        if step.get("error_actions"):
            _validate_steps(step["error_actions"], where=f"{label}.error")


def _validate_digest(step, label):
    """Save-time check for a digest step (mirrors engine.logic._run_digest).

    ``mode`` is accumulate (the engine default) or flush; ``key`` is
    required and names the digest — a literal, since the accumulate and
    flush runs are different events and the key must not depend on either.
    Accumulate needs something to append: ``item`` (a template string)
    and/or a non-empty ``items`` list of templates; flush takes neither
    (it drains what earlier runs appended). At run time the engine
    validates the rendered items; only shape is checked here.
    """
    from ..engine import logic

    mode = str(step.get("mode") or "accumulate").strip().lower() or "accumulate"
    if mode not in logic.DIGEST_MODES:
        raise WorkflowError(
            f"step '{label}': digest mode must be one of {', '.join(logic.DIGEST_MODES)}")
    if not str(step.get("key") or "").strip():
        raise WorkflowError(f"step '{label}': digest requires a key")
    if "shared" in step and not isinstance(step["shared"], bool):
        raise WorkflowError(f"step '{label}': digest shared must be true or false")
    has_item = step.get("item") is not None
    items = step.get("items")
    if has_item and not isinstance(step["item"], str):
        raise WorkflowError(f"step '{label}': digest item must be a template string")
    if items is not None and (
            not isinstance(items, list) or not items
            or not all(isinstance(entry, str) for entry in items)):
        raise WorkflowError(
            f"step '{label}': digest items must be a non-empty list of template strings")
    if mode == "accumulate" and not has_item and items is None:
        raise WorkflowError(
            f"step '{label}': digest accumulate needs an item or a non-empty items list")
    if mode == "flush" and (has_item or items is not None):
        raise WorkflowError(
            f"step '{label}': digest flush drains the batch; item/items only "
            "apply to accumulate")


def _validate_delay(step, label):
    """Save-time check for a delay step (mirrors engine.logic._delay_request).

    Durations — ``days``/``hours``/``minutes``/``seconds`` — combine; each is
    a non-negative number or a ``{template}`` rendered when the step runs.
    ``until`` is an ISO 8601 datetime (or a ``{template}``) and excludes the
    duration fields. At least one positive duration, or ``until``, is
    required. Totals up to DELAY_MAX_DAYS are legal: past the engine's 60s
    inline sleep cap the run suspends and the queue resumes it, so the old
    "max 60 seconds" bound no longer applies. Template values can only be
    checked for shape here — the engine validates the rendered numbers.
    """
    from ..engine import logic

    total = 0.0
    templated = False
    for key in logic.DURATION_KEYS:
        value = step.get(key)
        if value is None:
            continue
        if isinstance(value, str):
            if DELAY_TEMPLATE.match(value.strip()):
                templated = True
                continue
            try:
                value = float(value.strip())
            except ValueError:
                raise WorkflowError(
                    f"step '{label}': delay {key} must be a number or a "
                    f"{{template}} (got '{value}')") from None
        if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
            raise WorkflowError(
                f"step '{label}': delay {key} must be a non-negative number "
                "or a {template}")
        total += float(value) * logic.DURATION_UNITS[key]
    until = step.get("until")
    if until is not None:
        if total > 0 or templated:
            raise WorkflowError(
                f"step '{label}': set delay until or a duration, not both")
        if isinstance(until, str) and not DELAY_TEMPLATE.match(until.strip()) \
                and logic.parse_moment(until) is None:
            raise WorkflowError(
                f"step '{label}': delay until must be an ISO 8601 datetime "
                "or a {template}")
        return
    if total <= 0 and not templated:
        raise WorkflowError(
            f"step '{label}': delay needs seconds (or minutes, hours, days, "
            "or until) as a positive number")
    if total > DELAY_MAX_DAYS * 86400:
        raise WorkflowError(
            f"step '{label}': a delay may not exceed {DELAY_MAX_DAYS} days")


def get_token():
    secret_id = os.environ.get(TOKEN_SECRET_ENV, "").strip()
    if not secret_id:
        raise SyncConfigError(
            "git sync is not configured: store a GitHub token (Contents: read/write) "
            "in Secrets Manager and set GithubWorkflowsTokenSecret at deploy time"
        )
    import boto3

    value = boto3.client("secretsmanager").get_secret_value(SecretId=secret_id)["SecretString"]
    token = str(value).strip()
    if not token:
        raise SyncConfigError(f"the secret {secret_id} is empty")
    return token


def _github(method, path, token, payload=None):
    """One GitHub API call; non-2xx becomes SyncError with the API's message."""
    request = urllib.request.Request(
        f"{GITHUB_API}{path}",
        data=json.dumps(payload).encode() if payload is not None else None,
        headers={
            "accept": "application/vnd.github+json",
            "authorization": f"Bearer {token}",
            "content-type": "application/json",
            "user-agent": "dapier-designer",
        },
        method=method,
    )
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            body = response.read()
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:300]
        raise SyncError(f"github refused {method} {path}: HTTP {exc.code} {detail}") from exc
    except (urllib.error.URLError, TimeoutError) as exc:
        raise SyncError(f"github request failed: {exc}") from exc
    return json.loads(body) if body else {}


def _tree_entry_write(filename, yaml_text):
    return {"path": f"workflows/{filename}", "mode": "100644", "type": "blob", "content": yaml_text}


def _tree_entry_delete(filename):
    # sha: null with base_tree removes the entry.
    return {"path": f"workflows/{filename}", "mode": "100644", "type": "blob", "sha": None}


def commit_workflow(yaml_text, *, message, rename_from=None, token=None):
    """Validate and commit the workflow (create/update, optionally renaming).

    Returns the commit sha, its html_url, and the final file name. The whole
    change — write plus optional delete of rename_from — is one atomic commit.
    """
    workflow = parse_workflow(yaml_text)
    filename = filename_for(workflow["id"])
    if rename_from is not None and not FILE_PATTERN.fullmatch(rename_from):
        raise WorkflowError(f"invalid source file name: {rename_from!r}")

    token = token if token is not None else get_token()
    slug = repo_slug()
    ref = _github("GET", f"/repos/{slug}/git/ref/heads/{branch()}", token)
    base_commit = ref["object"]["sha"]
    base = _github("GET", f"/repos/{slug}/git/commits/{base_commit}", token)

    tree = [_tree_entry_write(filename, yaml_text)]
    if rename_from is not None and rename_from != filename:
        tree.append(_tree_entry_delete(rename_from))
    new_tree = _github(
        "POST", f"/repos/{slug}/git/trees", token,
        payload={"base_tree": base["tree"]["sha"], "tree": tree},
    )
    commit = _github(
        "POST", f"/repos/{slug}/git/commits", token,
        payload={"message": message, "tree": new_tree["sha"], "parents": [base_commit]},
    )
    _github("PATCH", f"/repos/{slug}/git/refs/heads/{branch()}", token, payload={"sha": commit["sha"]})
    return {
        "file": filename,
        "removed": rename_from if rename_from is not None and rename_from != filename else None,
        "commit": commit["sha"],
        "html_url": commit.get("html_url"),
    }


def fetch_workflow(filename, *, token=None):
    """The committed YAML for one file, straight from GitHub (no deploy wait)."""
    if not FILE_PATTERN.fullmatch(filename):
        raise WorkflowError(f"invalid workflow file name: {filename!r}")
    token = token if token is not None else get_token()
    slug = repo_slug()
    try:
        payload = _github(
            "GET",
            f"/repos/{slug}/contents/workflows/{filename}?ref={branch()}",
            token,
        )
    except SyncError as exc:
        if "HTTP 404" in str(exc):
            raise KeyError(filename) from exc
        raise
    content = payload.get("content") or ""
    if payload.get("encoding") != "base64" or not content:
        raise SyncError(f"github returned an unexpected payload for {filename}")
    return yaml.safe_load(base64.b64decode(content).decode())
