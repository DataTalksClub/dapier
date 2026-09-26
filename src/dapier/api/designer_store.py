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
DELAY_MAX_SECONDS = 60
LOOP_MAX_ITERATIONS = 100
FILTER_OPERATORS = ("equals", "in", "prefix", "suffix", "contains")


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
        "source": source,
        "connector": str(primary.get("connector", "?")),
        "event": str(primary.get("event", "?")),
        "triggerCount": len(triggers),
        "actionCount": len(actions or []),
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


def api_list():
    """Bundled workflows with the live published state overlaid by id.

    A workflow saved but not yet picked up by the deploy pipeline shows up
    here too — its published state is what actually runs.
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
    return 200, {"workflows": ordered, **sync_status()}


def api_get(source):
    """One workflow: the live published state first, then the bundle, then
    committed git state (covering a save whose deploy has not finished).
    """
    item = _published_by_file(source)
    if item and isinstance(item.get("workflow"), dict):
        return 200, {"workflow": item["workflow"], "published": True}
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
    return 200, {"workflow": workflow, "published": False}


def api_save(body, operator=None):
    """Validate and commit a workflow definition, then publish it live.

    Returns (status, payload). ``published`` in the payload says whether the
    definition is already running; a publish failure after a successful commit
    is a loud 502 (the commit sha is included), and retrying the save is safe.
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
            message=f"designer: save workflow {workflow['id']}",
        )
    except WorkflowError as exc:
        return 400, {"error": str(exc)}
    except SyncConfigError as exc:
        return 503, {"error": str(exc)}
    except SyncError as exc:
        return 502, {"error": str(exc)}
    return _publish_committed(workflow, result, rename_from=rename_from, operator=operator)


def _publish_committed(workflow, result, *, rename_from=None, operator=None):
    """Publish the just-committed definition; also drop a renamed-away id."""
    if not published_workflows.configured():
        return 200, {**result, "published": False}
    try:
        previous = published_workflows.get_item(workflow["id"])
        published_workflows.publish(workflow, operator=operator, previous=previous)
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
        published_workflows.publish(workflow, operator=operator, previous=previous)
    except Exception as exc:
        return 502, {"error": f"publish failed: {exc}"}
    result = {
        "file": source,
        "enabled": body["enabled"],
        "published": True,
    }
    try:
        committed = commit_workflow(
            yaml.safe_dump(ordered_workflow(workflow), sort_keys=False),
            message=f"designer: {'enable' if body['enabled'] else 'disable'} workflow {workflow['id']}",
        )
        result["commit"] = committed["commit"]
    except (SyncConfigError, SyncError) as exc:
        result["git_sync_error"] = str(exc)
    return 200, result


def filename_for(workflow_id):
    return f"{workflow_id}.yaml"


def api_test_run(source, body, operator=None):
    """Test-run one workflow against a sample event: dry-run by default,
    real execution with ``execute: true``.

    The workflow under test is, in order: the inline draft definition in the
    body (``workflow`` object or ``yaml`` text — the point of the feature is
    testing what is about to be saved), else the current file (published
    overlay / bundled YAML / committed git state). ``event`` is the sample
    event payload. Read-only unless ``execute`` is set.
    """
    del operator  # recorded by the calling route; the run itself is read-only
    if not isinstance(body, dict):
        return 400, {"error": "request body must be an object"}
    sample = body.get("event")
    if not isinstance(sample, dict):
        return 400, {"error": 'body must include "event": the sample event object'}
    if not isinstance(body.get("execute", False), bool):
        return 400, {"error": "execute must be a boolean"}
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
            label = filename_for(str(workflow["id"]))
        elif isinstance(yaml_text, str):
            workflow = parse_workflow(yaml_text)
            label = filename_for(str(workflow["id"]))
        else:
            if not source:
                return 400, {"error": "pass the workflow inline (workflow or yaml) "
                                      "or test a saved file: /workflows/{file}/test"}
            status, payload = api_get(source)
            if status != 200:
                return status, payload
            workflow = payload["workflow"]
            label = source
    except WorkflowError as exc:
        return 400, {"error": str(exc)}

    from ..engine import dryrun

    try:
        payload = dryrun.test_run(workflow, sample, execute=bool(body.get("execute", False)))
    except dryrun.TestRunError as exc:
        return 400, {"error": str(exc)}
    payload["file"] = label
    return 200, payload


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
    logic step kinds — filter, condition, delay, for_each — are checked here
    so a bad delay bound or an empty loop body fails the save with a clear
    error instead of failing at run time.
    """
    from ..engine import logic

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
        if kind == "delay":
            seconds = step.get("seconds")
            if isinstance(seconds, bool) or not isinstance(seconds, (int, float)) \
                    or seconds <= 0 or seconds > DELAY_MAX_SECONDS:
                raise WorkflowError(
                    f"step '{label}': delay seconds must be a number between 1 and "
                    f"{DELAY_MAX_SECONDS} (Lambda invocation limit)")
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
