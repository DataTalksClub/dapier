"""Agent connector: hand a rendered prompt to the host worker."""
from ..engine.actions.agent import run_agent
from .registry import Action, register

register(Action(
    type="agent",
    label="Agent",
    icon="bot",
    description=("Queue a prompt for a headless host worker run. The trigger's "
                 "stored attachments (an email's files) are staged into the "
                 "agent's workspace under attachments/ and the run is told "
                 "where they landed; attachments: off skips them."),
    run=lambda action, event, workflow_id, steps=None: run_agent(
        action, event, workflow_id, steps),
    required=frozenset({"prompt"}),
    optional=frozenset({"workspace", "engine", "tag_prefix", "attachments",
                        "notify_to", "notify_from", "requires", "id"}),
    fields=(
        {"key": "prompt", "label": "Prompt", "type": "textarea", "required": True,
         "placeholder": "{subject}\n\n{text}"},
        {"key": "workspace", "label": "Workspace (optional)",
         "placeholder": "defaults to the host worker's workspace root"},
        {"key": "engine", "label": "Engine", "placeholder": "claude"},
        {"key": "requires", "label": "Required capabilities",
         "placeholder": "browser", "description": "Comma-separated; all must match the worker"},
        {"key": "tag_prefix", "label": "Tag prefix", "placeholder": "agent"},
        {"key": "attachments", "label": "Pass trigger attachments",
         "type": "select", "options": ["default", "off"], "default": "default",
         "description": "off keeps the trigger's files out of the workspace"},
        {"key": "notify_to", "label": "Completion email (optional)",
         "placeholder": "defaults to the sender for email triggers"},
        {"key": "notify_from", "label": "Completion email From (optional)",
         "placeholder": "defaults to the deployment sender"},
    ),
))
