"""Agent connector: hand a rendered prompt to the host worker."""
from ..engine.actions.agent import run_agent
from .registry import Action, register

register(Action(
    type="agent",
    label="Agent",
    icon="bot",
    description="Queue a prompt for a headless host worker run.",
    run=lambda action, event, workflow_id, steps=None: run_agent(
        action, event, workflow_id, steps),
    required=frozenset({"prompt"}),
    optional=frozenset({"workspace", "engine", "tag_prefix", "notify_to", "id"}),
    fields=(
        {"key": "prompt", "label": "Prompt", "type": "textarea", "required": True,
         "placeholder": "{subject}\n\n{text}"},
        {"key": "workspace", "label": "Workspace (optional)",
         "placeholder": "defaults to the host worker's workspace root"},
        {"key": "engine", "label": "Engine", "placeholder": "claude"},
        {"key": "tag_prefix", "label": "Tag prefix", "placeholder": "agent"},
        {"key": "notify_to", "label": "Completion email (optional)",
         "placeholder": "defaults to the sender for email triggers"},
    ),
))
