"""Email connector: send via SES."""
from ..engine.actions.email import run_email_send
from .registry import Action, register

register(Action(
    type="email_send",
    label="Send email",
    icon="mail",
    description=("Send an email through SES. in_reply_to and references "
                 "thread a reply (their values land as the In-Reply-To and "
                 "References MIME headers; a send carrying either rides "
                 "through raw MIME, like attachment sends). Output: "
                 "{message_id, to, subject} plus cc/bcc/in_reply_to when set."),
    run=lambda action, event, workflow_id, steps=None: run_email_send(action, event, steps=steps),
    required=frozenset({"to"}),
    optional=frozenset({"subject", "text", "html", "sender",
                        "reply_to", "cc", "bcc", "attachments",
                        "in_reply_to", "references"}),
    fields=(
        {"key": "to", "label": "To", "type": "email", "required": True,
         "placeholder": "you@example.com or {sender}"},
        {"key": "subject", "label": "Subject", "placeholder": "{subject}"},
        {"key": "text", "label": "Text body", "type": "textarea"},
        {"key": "html", "label": "HTML body", "type": "textarea"},
        {"key": "sender", "label": "Sender", "type": "email",
         "placeholder": "defaults to the workflow sender"},
        {"key": "reply_to", "label": "Reply-To", "type": "email",
         "placeholder": "replies@example.com or a list"},
        {"key": "in_reply_to", "label": "In-Reply-To",
         "placeholder": "{trigger.message_id}",
         "help": "Message-ID this message replies to — mail clients thread "
                 "on it; templated"},
        {"key": "references", "label": "References",
         "placeholder": "{trigger.message_id}",
         "help": "The thread's Reference chain (space-separated Message-IDs) "
                 "— templated"},
        {"key": "cc", "label": "Cc", "type": "email",
         "placeholder": "copy@example.com, other@example.com"},
        {"key": "bcc", "label": "Bcc", "type": "email",
         "placeholder": "hidden@example.com"},
        {"key": "attachments", "label": "Attachments (YAML)", "type": "textarea",
         "placeholder": ("- filename: report.pdf\n"
                         "  source_url: https://example.test/report.pdf\n"
                         "- filename: note.txt\n"
                         "  content: plain text")},
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
# connectors.ingress._normalize_email) plus the decoded text/html bodies the
# raw-MIME intake publishes (triggers.intake.email_ingress), filled with an
# invoice-style message so template previews render true field shapes, not
# {"key": "value"} stubs.
_EMAIL_SYNTHETIC_DATA = {
    "route": "todo@dtcdev.click",
    "message_id": "<discover-00000000@dtcdev.click>",
    "sender": {"addresses": ["billing@example.test"],
               "header": "Acme Billing <billing@example.test>"},
    "recipients": {"matched": ["todo@dtcdev.click"]},
    "subject": "Invoice #4137 - September",
    "date": "2026-09-27T10:15:00Z",
    "text": "Invoice #4137 for September is attached.",
    "html": "<p>Invoice #4137 for September is attached.</p>",
    "body": {"html": {"value": "<p>Invoice #4137 for September is attached.</p>"}},
    "attachments": [],
    "raw_mime": {"bucket": "dapier-mail-inbound", "key": "raw/discover-sample"},
}


register_trigger_discovery(TriggerDiscovery(
    connector="email", label="Email", kind="sample", resource="",
    fetch=history_or_synthetic_fetch("email", "message.received", _EMAIL_SYNTHETIC_DATA)))
