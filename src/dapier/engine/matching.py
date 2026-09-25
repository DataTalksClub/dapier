"""Workflow loading and trigger matching."""
import logging
import os
from functools import lru_cache
from pathlib import Path

import yaml


logger = logging.getLogger(__name__)


def _root():
    return Path(os.environ.get("WORKFLOWS_DIR", Path(__file__).resolve().parents[3] / "workflows"))


@lru_cache
def _documents(root):
    """Every YAML document in the bundle, keyed by directory so concurrent
    readers of different WORKFLOWS_DIR values never share cache entries."""
    return [doc for doc in (yaml.safe_load(path.read_text()) for path in sorted(root.glob("*.yaml")))
            if isinstance(doc, dict)]

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

@lru_cache
def _workflows(root):
    return [doc for doc in _documents(root) if doc.get("id") and workflow_triggers(doc)]

def workflows():
    return _workflows(_root())

@lru_cache
def _flows(root):
    """Named action chains shared across workflows and stored triggers.

    Any bundled file may carry a top-level ``flows:`` mapping of
    ``{description, actions}`` (a bare action list also works). Files load
    in sorted order; a name defined twice is last-write-wins.
    """
    catalog = {}
    for doc in _documents(root):
        for name, spec in (doc.get("flows") or {}).items():
            if isinstance(spec, list):
                spec = {"actions": spec}
            if not isinstance(spec, dict):
                logger.warning("flow '%s' is not a mapping; ignored", name)
                continue
            if name in catalog:
                logger.warning("flow '%s' is defined more than once; keeping the last one", name)
            catalog[str(name)] = spec
    return catalog

def flows():
    return _flows(_root())

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
    """Bundled workflows (flow references resolved), published overrides, and
    operator-created email, hook, and schedule triggers (read per invocation).

    Published workflows (designer saves) overlay the bundle by id, so a save —
    including its enabled flag — takes effect without a deploy. Their flow
    references resolve against the bundled flow catalog like any other.
    """
    resolved = [workflow for workflow in
                (resolve_workflow(doc) for doc in workflows())
                if workflow is not None]
    merged = {workflow["id"]: workflow for workflow in resolved}
    if os.environ.get("PUBLISHED_WORKFLOWS_TABLE"):
        from ..triggers import published_workflows

        for workflow in published_workflows.load_workflows():
            resolved_published = resolve_workflow(workflow)
            if resolved_published is not None:
                merged[resolved_published["id"]] = resolved_published
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
    return list(merged.values()) + extra

def _matches_filter(value, rule):
    text = "" if value is None else str(value)
    if not isinstance(rule, dict):
        return value == rule
    return all({
        "equals": lambda expected: text == str(expected),
        "in": lambda expected: isinstance(expected, list) and text in [str(item) for item in expected],
        "prefix": lambda expected: text.startswith(str(expected)),
        "suffix": lambda expected: text.endswith(str(expected)),
        "contains": lambda expected: str(expected) in text,
    }[operator](expected) for operator, expected in rule.items())

def matches(workflow, event):
    """True when any of the workflow's triggers matches the event."""
    if not workflow.get("enabled", True):
        return False
    data = event.get("data", {})
    for trigger in workflow_triggers(workflow):
        if trigger.get("connector") != event.get("connector") or trigger.get("event") != event.get("event"):
            continue
        if all(_matches_filter(data.get(field), rule)
               for field, rule in (trigger.get("filters") or {}).items()):
            return True
    return False
