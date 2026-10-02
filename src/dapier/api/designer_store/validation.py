"""Workflow YAML rules: parsing, id/folder/tag validation, and the
structural checks the engine re-enforces at runtime."""

import re
import yaml

def shared(name):
    """Read a module constant through the designer_store package namespace,
    so tests patching designer_store.<name> are honored everywhere."""
    import importlib

    return getattr(importlib.import_module(__package__), name)


class _LateBinding:
    """A callable resolved through the designer_store package at call time,
    so tests that patch designer_store.<name> are honored by every layer."""

    def __init__(self, name):
        self._name = name

    def __call__(self, *args, **kwargs):
        import importlib

        return getattr(importlib.import_module(__package__), self._name)(*args, **kwargs)


__all__ = ["AUTO_PAUSE_KEYS", "DELAY_MAX_DAYS", "DELAY_TEMPLATE", "FILE_PATTERN", "FILTER_OPERATORS", "ID_PATTERN", "LOOP_MAX_ITERATIONS", "MAX_FOLDER_LENGTH", "MAX_TAGS", "MAX_TAG_LENGTH", "MAX_YAML_BYTES", "WORKFLOW_KEY_ORDER", "WorkflowError", "filename_for", "ordered_workflow", "parse_workflow", "slugify_id"]


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

# Zapier-style workflow tags: a handful of short labels a workflow carries in
# its YAML, edited through the tags endpoint and used to filter the lists.
MAX_TAGS = 20
MAX_TAG_LENGTH = 64

# Zapier-style workflow folders: flat, unlike tags — a workflow sits in at
# most one folder (or none), and a folder is a name, never a path.
MAX_FOLDER_LENGTH = MAX_TAG_LENGTH

# Zapier-style auto-pause: the engine pauses a workflow after this many
# consecutive failed runs (the worker counts them; the YAML key
# ``auto_pause_after:`` overrides the default). The flag lives on the stored
# definition like ``enabled`` does, so it travels with every engine read.
AUTO_PAUSE_KEYS = ("auto_paused", "auto_paused_at", "auto_paused_reason")


def _validate_tags(tags):
    """Clean a tags list or raise WorkflowError: strings only, trimmed,
    non-empty, deduped case-insensitively (first spelling wins), bounded in
    count and length. Shared by YAML parsing (a hand-written ``tags:``) and
    the tags endpoint."""
    if not isinstance(tags, list):
        raise WorkflowError("tags must be a list of strings")
    if len(tags) > MAX_TAGS:
        raise WorkflowError(f"a workflow carries at most {MAX_TAGS} tags")
    clean = []
    for tag in tags:
        if not isinstance(tag, str):
            raise WorkflowError("tags must be a list of strings")
        text = tag.strip()
        if not text:
            raise WorkflowError("tags must be non-empty strings")
        if len(text) > MAX_TAG_LENGTH:
            raise WorkflowError(f"each tag may be at most {MAX_TAG_LENGTH} characters")
        if text.lower() not in {existing.lower() for existing in clean}:
            clean.append(text)
    return clean


def _validate_folder(folder):
    """A folder value as it should be stored, or raise WorkflowError: a
    string, stripped, at most MAX_FOLDER_LENGTH characters, and never a
    path — Zapier folders are flat (a workflow sits in at most one), so
    ``/`` and ``\\`` cannot appear. Empty means "no folder". Shared by YAML
    parsing (a hand-written ``folder:``) and the folder endpoint."""
    if not isinstance(folder, str):
        raise WorkflowError("folder must be a string")
    text = folder.strip()
    if "/" in text or "\\" in text:
        raise WorkflowError("folder cannot contain / or \\ — folders are flat, not paths")
    if len(text) > MAX_FOLDER_LENGTH:
        raise WorkflowError(f"folder may be at most {MAX_FOLDER_LENGTH} characters")
    return text


class WorkflowError(ValueError):
    """Invalid workflow definition."""


def slugify_id(text):
    """A workflow id from free text: lowercase, runs of characters outside
    the id alphabet (everything but alphanumerics and underscores) collapse
    to one hyphen, and the result fits ID_PATTERN (alnum first, 63 chars
    max). Underscores survive ids like dropbox_on_upload.
    Empty when nothing usable survives."""
    slug = re.sub(r"[^a-z0-9_]+", "-", str(text).strip().lower())
    slug = re.sub(r"-+", "-", slug).strip("-")
    slug = re.sub(r"^[^a-z0-9]+", "", slug)
    return slug[:63].rstrip("-")


def filename_for(workflow_id):
    return f"{workflow_id}.yaml"


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

    if "allow_email_overlap" in workflow and not isinstance(workflow["allow_email_overlap"], bool):
        raise WorkflowError("allow_email_overlap must be true or false")

    tags = workflow.get("tags")
    if tags is not None:
        _validate_tags(tags)

    # A flat Zapier-style folder is optional; an empty one is no folder, so
    # the key is dropped rather than stored as "".
    folder = workflow.get("folder")
    if folder is not None:
        cleaned = _validate_folder(folder)
        if cleaned:
            workflow["folder"] = cleaned
        else:
            workflow.pop("folder", None)

    # The auto-pause threshold is optional: a positive integer count of
    # consecutive failed runs, or false/0 to switch the trip wire off
    # (normalized to false — dropping the key would mean "the default").
    # True means the default, so the key is dropped. Like tags/folder, the
    # same rules the engine applies at run time apply here at save time.
    auto_pause_after = workflow.get("auto_pause_after")
    if auto_pause_after is not None:
        if auto_pause_after is True:
            workflow.pop("auto_pause_after", None)
        elif auto_pause_after is False or auto_pause_after == 0:
            workflow["auto_pause_after"] = False
        elif isinstance(auto_pause_after, int) and not isinstance(auto_pause_after, bool) \
                and auto_pause_after > 0:
            pass
        else:
            raise WorkflowError(
                "auto_pause_after must be a positive number of consecutive "
                "failed runs, or false to switch the auto-pause off")

    # Retire the old gallery flag when importing or saving older definitions.
    workflow.pop("template", None)

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
        from ...engine import matching

        if matching.flow_actions(flow) is None:
            raise WorkflowError(f"no shared flow named '{flow}'")
    elif not isinstance(actions, list) or not actions:
        raise WorkflowError("connect at least one action to the trigger")
    for action in (actions or []):
        if not isinstance(action, dict) or not str(action.get("type") or "").strip():
            raise WorkflowError("every action needs a type")
        if action.get("type") == "code" and not str(action.get("code") or "").strip():
            raise WorkflowError("every code action needs non-empty code")
        from ...engine.actions import templating

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
    from ...engine import logic
    from ...connectors import registry

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
    from ...engine import logic

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
    from ...engine import logic

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
