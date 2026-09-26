"""Email connector: send via SES."""
from ..engine.actions.email import run_email_send
from .registry import Action, register

register(Action(
    type="email_send",
    label="Send email",
    icon="mail",
    run=lambda action, event, workflow_id, steps=None: run_email_send(action, event, steps=steps),
    required=frozenset({"to"}),
    optional=frozenset({"subject", "text", "html", "sender"}),
    fields=(
        {"key": "to", "label": "To", "required": True, "placeholder": "you@example.com or {sender}"},
        {"key": "subject", "label": "Subject", "placeholder": "{subject}"},
        {"key": "text", "label": "Text body", "type": "textarea"},
        {"key": "html", "label": "HTML body", "type": "textarea"},
        {"key": "sender", "label": "Sender", "placeholder": "defaults to the workflow sender"},
    ),
))
