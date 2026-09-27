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
        {"key": "to", "label": "To", "type": "email", "required": True,
         "placeholder": "you@example.com or {sender}"},
        {"key": "subject", "label": "Subject", "placeholder": "{subject}"},
        {"key": "text", "label": "Text body", "type": "textarea"},
        {"key": "html", "label": "HTML body", "type": "textarea"},
        {"key": "sender", "label": "Sender", "type": "email",
         "placeholder": "defaults to the workflow sender"},
    ),
))


# --- trigger discovery: the newest recorded message, else a realistic sample ---

from . import trigger_discovery
from .trigger_discovery import (
    DEFAULT_LIMIT,
    TriggerDiscovery,
    history_or_synthetic_fetch,
    register_trigger_discovery,
)

# The inbound-email contract the SES catch-all publishes (see
# connectors.ingress._normalize_email), filled with an invoice-style message
# so template previews render true field shapes, not {"key": "value"} stubs.
_EMAIL_SYNTHETIC_DATA = {
    "route": "todo@dtcdev.click",
    "message_id": "<discover-00000000@dtcdev.click>",
    "sender": {"addresses": ["billing@example.test"],
               "header": "Acme Billing <billing@example.test>"},
    "recipients": {"matched": ["todo@dtcdev.click"]},
    "subject": "Invoice #4137 - September",
    "date": "2026-09-27T10:15:00Z",
    "body": {"html": {"value": "<p>Invoice #4137 for September is attached.</p>"}},
    "attachments": [],
    "raw_mime": {"bucket": "dapier-mail-inbound", "key": "raw/discover-sample"},
}


register_trigger_discovery(TriggerDiscovery(
    connector="email", label="Email", kind="sample", resource="",
    fetch=history_or_synthetic_fetch("email", "message.received", _EMAIL_SYNTHETIC_DATA)))
