"""Agent connector: hand a rendered prompt to the host worker."""
from ..engine.actions.agent import run_agent
from .registry import Action, register

register(Action(
    type="agent",
    label="Agent",
    icon="bot",
    description="Queue a prompt for the host worker, which starts an Aplexer session.",
    run=lambda action, event, workflow_id, steps=None: run_agent(
        action, event, workflow_id, steps),
    required=frozenset({"prompt", "workspace"}),
    optional=frozenset({"engine", "tag_prefix", "id"}),
    fields=(
        {"key": "prompt", "label": "Prompt", "type": "textarea", "required": True,
         "placeholder": "{subject}\n\n{text}"},
        {"key": "workspace", "label": "Workspace", "required": True,
         "placeholder": "/home/alexey/git/dapier"},
        {"key": "engine", "label": "Engine", "placeholder": "claude"},
        {"key": "tag_prefix", "label": "Tag prefix", "placeholder": "agent"},
    ),
))
