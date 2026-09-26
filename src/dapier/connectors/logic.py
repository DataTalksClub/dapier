"""In-workflow logic steps, as catalog metadata for the designer.

These steps execute inside ``engine.logic`` (filter, condition, delay,
for_each), not through the connector dispatch — the registry carries their
inspector schema so ``GET /api/catalog`` is the complete step palette.
"""
from .registry import LogicStep, logic_step

logic_step(LogicStep(
    type="filter",
    label="Filter",
    icon="filter",
    description="Stop the chain quietly unless the condition holds",
    fields=(
        {"key": "field", "label": "Field", "placeholder": "subject", "required": True},
        {"key": "operator", "label": "Operator", "type": "select", "options": ["equals", "in", "prefix", "suffix", "contains"], "default": "equals"},
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
        {"key": "operator", "label": "Operator", "type": "select", "options": ["equals", "in", "prefix", "suffix", "contains"], "default": "equals"},
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
    description="Pause the chain before the next step (max 60s)",
    fields=(
        {"key": "seconds", "label": "Seconds (max 60)", "type": "number", "required": True, "placeholder": "30"},
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
