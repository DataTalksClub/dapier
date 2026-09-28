"""Workflow loading and trigger matching."""
import logging
import os


logger = logging.getLogger(__name__)


def workflow_triggers(workflow):
    """The trigger specs of a workflow: the ``triggers`` list, or the single
    ``trigger``. A workflow may list any number of triggers; they all share
    the workflow's action chain, so one common flow can serve several entry
    points (e.g. two invoice addresses and a Dropbox folder)."""
    triggers = workflow.get("triggers")
    if isinstance(triggers, list) and triggers:
        return [trigger for trigger in triggers if isinstance(trigger, dict)]
    trigger = workflow.get("trigger")
    return [trigger] if isinstance(trigger, dict) else []

def workflows():
    """Managed workflow definitions, read fresh for each invocation."""
    if not os.environ.get("PUBLISHED_WORKFLOWS_TABLE"):
        return []
    from ..triggers import published_workflows

    return [workflow for workflow in published_workflows.load_workflows()
            if workflow_triggers(workflow)]

def flows():
    """Legacy shared flows are retired; stored triggers carry their actions."""
    return {}

def flow_actions(name):
    """The actions of a named shared flow, or None when it is undefined or empty."""
    spec = flows().get(str(name or "").strip())
    if spec is None:
        return None
    actions = spec.get("actions")
    if not isinstance(actions, list) or not actions:
        logger.warning("flow '%s' has no actions", name)
        return None
    return actions

def flow_catalog():
    """Shared flows for list endpoints: name, description, action count."""
    return [
        {
            "name": name,
            "description": spec.get("description") or "",
            "actions": len(spec.get("actions") or []),
        }
        for name, spec in sorted(flows().items())
    ]

def resolve_workflow(workflow):
    """Fill in the actions of a ``flow``-bound workflow; None when the flow
    is undefined, so a removed flow fails its trigger closed (it stops
    matching) instead of running an empty action chain."""
    flow = str(workflow.get("flow") or "").strip()
    if not flow:
        return workflow
    actions = flow_actions(flow)
    if actions is None:
        logger.warning("workflow '%s' binds undefined flow '%s'; skipped",
                       workflow.get("id"), flow)
        return None
    return {**workflow, "actions": actions}

def all_workflows():
    """Managed workflows and operator-created triggers, read per invocation."""
    resolved = [workflow for workflow in
                (resolve_workflow(doc) for doc in workflows())
                if workflow is not None]
    extra = []
    if os.environ.get("EMAIL_TRIGGERS_TABLE"):
        from ..triggers import email_triggers

        extra = email_triggers.load_workflows()
    if os.environ.get("HOOK_TRIGGERS_TABLE"):
        from ..triggers import hook_triggers

        extra = extra + hook_triggers.load_workflows()
    if os.environ.get("SCHEDULE_TRIGGERS_TABLE"):
        from ..triggers import schedule_triggers

        extra = extra + schedule_triggers.load_workflows()
    if os.environ.get("POLL_TRIGGERS_TABLE"):
        from ..triggers import poll_triggers

        extra = extra + poll_triggers.load_workflows()
    return resolved + extra

def _ordered(left, right):
    """A three-way compare for the ordering operators: numeric when BOTH
    sides parse as float (so 9 < 10), else lexicographic — which is what
    makes ISO dates ("2026-09-28T...") compare correctly. Unknown operators
    and unparseable sides degrade to strings rather than erroring; a missing
    value reads as "" (which parses as neither, so it orders lexicographically
    below everything, like equals treats it as empty).
    """
    try:
        left, right = float(left), float(right)
    except (TypeError, ValueError):
        left, right = str(left), str(right)
    return (left > right) - (left < right)


def _present(text):
    return bool(text)


def _matches_filter(value, rule):
    """One filter rule: ``{operator: expected}`` (or a mapping of them, all
    required), with the value stringified first like every trigger filter.

    Operators: equals, not_equals, in, prefix, suffix, contains,
    does_not_contain, gt, lt, gte, lte (numeric when both sides parse as
    float, else lexicographic — see _ordered), exists and empty (present =
    non-empty after stringifying; the expected boolean flips the sense, so
    ``exists: true`` requires a value and ``empty: true`` requires its
    absence). An unknown operator raises KeyError — the callers turn that
    into a loud config error, never a quiet no-match.
    """
    text = "" if value is None else str(value)
    if not isinstance(rule, dict):
        return value == rule
    return all({
        "equals": lambda expected: text == str(expected),
        "in": lambda expected: isinstance(expected, list) and text in [str(item) for item in expected],
        "prefix": lambda expected: text.startswith(str(expected)),
        "suffix": lambda expected: text.endswith(str(expected)),
        "contains": lambda expected: str(expected) in text,
        "not_equals": lambda expected: text != str(expected),
        "does_not_contain": lambda expected: str(expected) not in text,
        "gt": lambda expected: _ordered(text, str(expected)) > 0,
        "gte": lambda expected: _ordered(text, str(expected)) >= 0,
        "lt": lambda expected: _ordered(text, str(expected)) < 0,
        "lte": lambda expected: _ordered(text, str(expected)) <= 0,
        "exists": lambda expected: _present(text) == bool(expected),
        "empty": lambda expected: _present(text) != bool(expected),
    }[operator](expected) for operator, expected in rule.items())

def matches(workflow, event):
    """True when any of the workflow's triggers matches the event. A disabled
    workflow never matches, and neither does an auto-paused one — the engine
    paused it after consecutive failed runs (the ``auto_paused`` flag on the
    stored definition, set by engine.worker, cleared by re-enabling)."""
    if not workflow.get("enabled", True) or workflow.get("auto_paused"):
        return False
    data = event.get("data", {})
    for trigger in workflow_triggers(workflow):
        if trigger.get("connector") != event.get("connector") or trigger.get("event") != event.get("event"):
            continue
        if all(_matches_filter(data.get(field), rule)
               for field, rule in (trigger.get("filters") or {}).items()):
            return True
    return False
