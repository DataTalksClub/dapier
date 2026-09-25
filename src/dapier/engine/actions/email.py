"""email_send action: send via SES with {field} templates."""
import os

from . import base


def run_email_send(action, event, *, ses=None):
    """Send an email through SES, with {field} templates filled from the event.

    The sender defaults to the configured workflow sender and then to the
    trigger domain's no-reply address; a per-action sender only works when SES
    verified that identity too.
    """
    if ses is None:
        import boto3

        ses = boto3.client("ses")
    from ...triggers.email_triggers import trigger_domain

    data = event.get("data", {})
    fields = data if isinstance(data, dict) else {}
    to = str(action.get("to") or "").format_map(base._SafeFormat(fields))
    addresses = [address.strip() for address in to.split(",") if address.strip()]
    if not addresses:
        raise ValueError("email_send needs a to address (literal or a {field} template)")
    text = str(action.get("text") or "").format_map(base._SafeFormat(fields))
    html = str(action.get("html") or "").format_map(base._SafeFormat(fields))
    if not text and not html:
        raise ValueError("email_send needs text or html")
    subject = str(action.get("subject") or "(no subject)").format_map(base._SafeFormat(fields))
    sender = (action.get("sender") or os.environ.get("DAPIER_EMAIL_SENDER")
              or f"no-reply@{trigger_domain()}")
    body = {}
    if text:
        body["Text"] = {"Data": text, "Charset": "utf-8"}
    if html:
        body["Html"] = {"Data": html, "Charset": "utf-8"}
    response = ses.send_email(
        Source=sender,
        Destination={"ToAddresses": addresses},
        Message={"Subject": {"Data": subject, "Charset": "utf-8"}, "Body": body},
    )
    return {"message_id": response.get("MessageId"), "to": addresses, "subject": subject}
