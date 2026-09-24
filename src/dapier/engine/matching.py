"""Workflow loading and trigger matching."""
import os
from functools import lru_cache
from pathlib import Path

import yaml


@lru_cache
def workflows():
    root = Path(os.environ.get("WORKFLOWS_DIR", Path(__file__).resolve().parents[3] / "workflows"))
    return [yaml.safe_load(path.read_text()) for path in sorted(root.glob("*.yaml"))]

def all_workflows():
    """YAML workflows plus operator-created email, hook, and schedule triggers (read per invocation)."""
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
    return workflows() + extra

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
    trigger = workflow["trigger"]
    if not workflow.get("enabled", True):
        return False
    if trigger["connector"] != event.get("connector") or trigger["event"] != event.get("event"):
        return False
    return all(_matches_filter(event.get("data", {}).get(field), rule)
               for field, rule in trigger.get("filters", {}).items())
