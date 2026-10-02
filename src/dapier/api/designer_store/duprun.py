"""Duplicates and dry runs: duplicate a workflow, test-run one, and
test a single step against the draft under test."""
import os

import yaml

from ...triggers import published_workflows
from .github import SyncConfigError, SyncError, TOKEN_SECRET_ENV
from .validation import (AUTO_PAUSE_KEYS, FILE_PATTERN, ID_PATTERN,
                         WorkflowError, _LateBinding, filename_for,
                         parse_workflow, slugify_id)
from .drafts import api_save
from .history import RUN_STATE_KEYS
from .listing import api_get, workflow_yaml_text


def _workflow_exists(workflow_id, filename):
    """Whether a workflow already answers to this id/file: the deployed
    bundle, the live published store, then committed git state (a save whose
    deploy has not finished). A git probe failure cannot be answered, so it
    does not count as existing — the commit that follows reports its own
    errors."""
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
    untouched. The source resolves by file name or bare id — a hook-backed
    workflow has no file, so duplicating one (into an editable managed copy)
    addresses it by id.

    ``body`` optionally carries ``{"name": "..."}``; without it the copy is
    named ``<id>-copy`` (the workflow's id is its name). Returns api_save's
    response shape plus ``duplicated_from``.
    """
    workflow_id = str(source or "").removesuffix(".yaml")
    if not (FILE_PATTERN.fullmatch(source or "") or ID_PATTERN.fullmatch(workflow_id)):
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
    # Duplicating publishes the copy live — a copy nobody can see or run is
    # not a duplicate; the original's drafts are untouched.
    status, payload = api_save(
        {"yaml": yaml_text}, operator=operator, live=True,
        message=f"designer: duplicate workflow {workflow['id']} as {new_id}")
    payload["duplicated_from"] = source
    return status, payload


def _workflow_under_test(source, body):
    """The workflow a test targets: ``(status, payload)``.

    The inline draft definition in the body (``workflow`` object or ``yaml``
    text — the point of the feature is testing what is about to be saved),
    else the current published definition. Success carries
    ``{"workflow": ..., "label": ...}``; anything
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

    from ...engine import dryrun

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

    from ...engine import dryrun

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
WORKFLOW_KEY_ORDER = ("id", "enabled", "trigger", "triggers", "actions", "flows", "flow")


# The git-sync seams resolve through the package at call time, so tests
# patching designer_store.<name> reach every caller; the facade re-binds
# the real implementations after the star imports.
fetch_workflow = _LateBinding("fetch_workflow")
