"""Hub intake connector: file a Hub task and optionally wake Developer."""
from ..engine.actions.hub_intake import run_hub_intake
from .registry import Action, register

register(Action(
    type="hub_intake",
    label="Hub intake",
    icon="inbox",
    run=lambda action, event, workflow_id, steps=None: run_hub_intake(action, event, steps=steps),
    required=frozenset({"title"}),
    optional=frozenset({
        "text", "project", "key", "source_type", "source_reference", "wake",
    }),
    fields=(
        {"key": "title", "label": "Title", "required": True},
        {"key": "text", "label": "Body (templated)", "type": "textarea",
         "placeholder": "Event for Developer"},
        {"key": "project", "label": "Hub project", "default": "dapier"},
        {"key": "key", "label": "Idempotency key (templated)"},
        {"key": "source_type", "label": "Source type", "type": "select",
         "options": ["chat", "voice", "forwarded", "unknown"], "default": "forwarded"},
        {"key": "source_reference", "label": "Source reference (templated)"},
        {"key": "wake", "label": "Assign/wake after create", "type": "select",
         "options": ["true", "false"], "default": "true"},
    ),
))
