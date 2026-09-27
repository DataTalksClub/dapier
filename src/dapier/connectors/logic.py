"""In-workflow logic steps, as catalog metadata for the designer.

These steps execute inside ``engine.logic`` (filter, condition, paths,
delay, for_each, digest), not through the connector dispatch — the registry carries their
inspector schema so ``GET /api/catalog`` is the complete step palette.
"""
from .registry import LOGIC_OPERATORS, LogicStep, logic_step

logic_step(LogicStep(
    type="filter",
    label="Filter",
    icon="filter",
    description="Stop the chain quietly unless the condition holds",
    fields=(
        {"key": "field", "label": "Field", "placeholder": "subject", "required": True},
        {"key": "operator", "label": "Operator", "type": "select",
         "options": list(LOGIC_OPERATORS), "default": "equals"},
        {"key": "value", "label": "Value", "placeholder": "invoice"},
    ),
))

logic_step(LogicStep(
    type="condition",
    label="Condition",
    icon="git-branch",
    description="Run the then steps, or the else steps",
    fields=(
        {"key": "field", "label": "Field", "placeholder": "route", "required": True},
        {"key": "operator", "label": "Operator", "type": "select",
         "options": list(LOGIC_OPERATORS), "default": "equals"},
        {"key": "value", "label": "Value"},
        {"key": "then", "label": "Then steps (YAML)", "type": "yaml",
         "placeholder": "- id: notify\n  type: slack\n  channel: \"#alerts\"\n  text: \"{subject}\""},
        {"key": "else", "label": "Else steps (YAML)", "type": "yaml"},
    ),
))

logic_step(LogicStep(
    type="delay",
    label="Delay",
    icon="timer",
    description="Pause the chain before the next step — over 60s the run "
                "suspends and the queue resumes it automatically",
    fields=(
        {"key": "seconds", "label": "Seconds", "type": "number", "placeholder": "30"},
        {"key": "minutes", "label": "Minutes", "type": "number"},
        {"key": "hours", "label": "Hours", "type": "number"},
        {"key": "days", "label": "Days", "type": "number"},
        {"key": "until", "label": "Until (ISO datetime)", "placeholder": "2026-10-01T09:00:00Z"},
    ),
))

logic_step(LogicStep(
    type="for_each",
    label="For each",
    icon="list-tree",
    description="Run steps once per item of a list (max 100 items)",
    fields=(
        {"key": "list", "label": "List field", "placeholder": "attachments", "required": True},
        {"key": "item", "label": "Item variable", "default": "item"},
        {"key": "max_iterations", "label": "Max iterations (max 100)", "type": "number"},
        {"key": "actions", "label": "Steps per item (YAML)", "type": "yaml",
         "placeholder": "- id: upload\n  type: dropbox_upload\n  connection_id: dropbox\n  folder: \"/Invoices/{item.filename}\""},
    ),
))

logic_step(LogicStep(
    type="paths",
    label="Paths",
    icon="git-branch",
    description="Run the first matching branch's steps, or a default",
    fields=(
        {"key": "paths", "label": "Paths (YAML)", "type": "yaml", "required": True,
         "placeholder": "- label: invoices\n  when: {subject: {contains: invoice}}\n  actions:\n    - id: notify\n      type: slack\n      channel: \"#alerts\"\n      text: \"{subject}\""},
        {"key": "default", "label": "Default steps (YAML)", "type": "yaml"},
    ),
))

logic_step(LogicStep(
    type="digest",
    label="Digest",
    icon="layers",
    description="Accumulate items across runs, then flush them as one batch "
                "(a schedule trigger usually fires the flush)",
    fields=(
        {"key": "mode", "label": "Mode", "type": "select",
         "options": ["accumulate", "flush"], "default": "accumulate"},
        {"key": "key", "label": "Digest key", "placeholder": "nightly-invoices", "required": True},
        {"key": "item", "label": "Item (accumulate)", "type": "textarea",
         "placeholder": "{subject}"},
        {"key": "items", "label": "Items (accumulate, YAML)", "type": "yaml",
         "placeholder": '- "{subject}"\n- "{trigger.occurred_at}"'},
        {"key": "shared", "label": "Shared across workflows", "type": "boolean",
         "default": False,
         "help": "Accumulate in one workflow, flush from the schedule-triggered one"},
    ),
))
