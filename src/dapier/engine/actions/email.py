"""email_send action: send via SES with {field} templates.

A plain send stays on SES's simple ``send_email`` shape — same Source,
Destination and Message as before, so existing workflows are untouched.
``reply_to``/``cc``/``bcc`` slot into that same call (``ReplyToAddresses``,
``Destination.CcAddresses``/``BccAddresses``). A send with ``attachments``
switches to ``send_raw_email`` with a MIME message built by
``email.message.EmailMessage`` — text/HTML parts, the extra recipients, and
one part per attachment with its guessed content type. The raw message
always carries an explicit ``Message-ID``: the inbound side dedupes on it
(see intake/email_ingress), and a message without one falls back to its
storage key as the identity.
"""
import mimetypes
import os
import uuid

from . import base
from .templating import render

DOWNLOAD_TIMEOUT = 30


def run_email_send(action, event, *, ses=None, steps=None, transport=None):
    """Send an email through SES, with {field} templates filled from the event
    (plus {trigger.*} and {steps.*} — see templating).

    The sender defaults to the configured workflow sender and then to the
    trigger domain's no-reply address; a per-action sender only works when SES
    verified that identity too.

    ``reply_to``, ``cc`` and ``bcc`` each take one address, a comma-separated
    string, or a list (every entry templated). ``attachments`` is a list of
    ``{filename, content}`` (literal UTF-8 text, not templated) or
    ``{filename, source_url}`` (one HTTP download, like s3_upload's
    ``source_url``); any send with attachments rides through raw MIME.

    ``in_reply_to`` and ``references`` thread a reply: they are set as the
    ``In-Reply-To`` and ``References`` MIME headers, templated (the
    triggering message's ``message_id`` is the usual source). SES's simple
    ``send_email`` call carries only the fixed header set — no custom
    headers — so a send with either threading header rides through raw MIME
    too, exactly like an attachment send; the headers land on both body
    shapes (text, HTML, with or without attachments) because
    :func:`_raw_message` stamps them for every raw send.
    """
    if ses is None:
        import boto3

        ses = boto3.client("ses")
    from ...triggers.email_triggers import trigger_domain

    to = render(action.get("to") or "", event, steps)
    addresses = [address.strip() for address in to.split(",") if address.strip()]
    if not addresses:
        raise ValueError("email_send needs a to address (literal or a {field} template)")
    text = render(action.get("text") or "", event, steps)
    html = render(action.get("html") or "", event, steps)
    if not text and not html:
        raise ValueError("email_send needs text or html")
    subject = render(action.get("subject") or "(no subject)", event, steps)
    sender = (action.get("sender") or os.environ.get("DAPIER_EMAIL_SENDER")
              or f"no-reply@{trigger_domain()}")
    reply_to = _addresses(action.get("reply_to"), event, steps)
    cc = _addresses(action.get("cc"), event, steps)
    bcc = _addresses(action.get("bcc"), event, steps)
    attachments = _attachments(action.get("attachments"), event, steps, transport=transport)
    in_reply_to = render(str(action.get("in_reply_to") or ""), event, steps).strip()
    references = render(str(action.get("references") or ""), event, steps).strip()

    if attachments or in_reply_to or references:
        message = _raw_message(sender, addresses, cc, bcc, reply_to, subject,
                               text, html, attachments, trigger_domain(),
                               in_reply_to=in_reply_to, references=references)
        response = ses.send_raw_email(RawMessage={"Data": message.as_bytes()})
    else:
        destination = {"ToAddresses": addresses}
        if cc:
            destination["CcAddresses"] = cc
        if bcc:
            destination["BccAddresses"] = bcc
        kwargs = {
            "Source": sender,
            "Destination": destination,
            "Message": {"Subject": {"Data": subject, "Charset": "utf-8"}, "Body": _body_parts(text, html)},
        }
        if reply_to:
            kwargs["ReplyToAddresses"] = reply_to
        response = ses.send_email(**kwargs)
    output = {"message_id": response.get("MessageId"), "to": addresses, "subject": subject}
    if cc:
        output["cc"] = cc
    if bcc:
        output["bcc"] = bcc
    if in_reply_to:
        output["in_reply_to"] = in_reply_to
    return output


def _body_parts(text, html):
    """The simple-format Body dict: Text, Html, or both."""
    body = {}
    if text:
        body["Text"] = {"Data": text, "Charset": "utf-8"}
    if html:
        body["Html"] = {"Data": html, "Charset": "utf-8"}
    return body


def _addresses(value, event, steps):
    """Recipients from one address, a comma-separated string, or a list."""
    if not value:
        return []
    if isinstance(value, (list, tuple)):
        rendered = ",".join(str(render(str(item), event, steps)) for item in value)
    else:
        rendered = str(render(str(value), event, steps))
    return [address.strip() for address in rendered.split(",") if address.strip()]


def _attachments(value, event, steps, *, transport=None):
    """The attachment bytes: literal ``content`` or one ``source_url`` download.

    Mirrors s3_upload's sourcing dialect — a failed download is a clear
    RuntimeError naming what was unreachable, never a truncated attachment.
    """
    if not value:
        return []
    if not isinstance(value, (list, tuple)):
        raise ValueError("email_send attachments must be a list of {filename, content|source_url}")
    attachments = []
    for index, entry in enumerate(value):
        if not isinstance(entry, dict):
            raise ValueError(f"attachment {index} must be an object with filename and content or source_url")
        filename = str(render(str(entry.get("filename") or ""), event, steps)).strip()
        if not filename:
            raise ValueError(f"attachment {index} needs a filename")
        content = entry.get("content")
        source_url = str(render(str(entry.get("source_url") or ""), event, steps)).strip()
        if content is None and not source_url:
            raise ValueError(f"attachment {filename} needs content or source_url")
        if content is not None and source_url:
            raise ValueError(f"attachment {filename} takes content or source_url, not both")
        if content is not None:
            data = content if isinstance(content, (bytes, bytearray)) else str(content).encode("utf-8")
        else:
            data = _download(source_url, transport)
        attachments.append((filename, bytes(data)))
    return attachments


def _download(url, transport):
    """The bytes behind a source_url — the s3_upload download dialect."""
    fetch = transport or base._default_transport
    try:
        status, body = fetch("GET", url, headers={}, body=None, timeout=DOWNLOAD_TIMEOUT)
    except Exception as exc:
        raise RuntimeError(f"attachment download unreachable: {type(exc).__name__}") from None
    if status >= 300:
        raise RuntimeError(f"attachment download returned HTTP {status}")
    return body


def _raw_message(sender, to, cc, bcc, reply_to, subject, text, html, attachments,
                 domain, in_reply_to=None, references=None):
    """The MIME message for a send with attachments — or threading headers:
    text/HTML parts, the extra recipients, one part per attachment, an
    explicit Message-ID, and the ``In-Reply-To``/``References`` headers when
    given (SES's simple call cannot carry custom headers, so threaded sends
    ride through here too)."""
    from email.message import EmailMessage

    message = EmailMessage()
    message["From"] = sender
    message["To"] = ", ".join(to)
    if cc:
        message["Cc"] = ", ".join(cc)
    if bcc:
        message["Bcc"] = ", ".join(bcc)
    if reply_to:
        message["Reply-To"] = ", ".join(reply_to)
    message["Subject"] = subject
    message["Message-ID"] = f"<{uuid.uuid4()}@{sender.rsplit('@', 1)[-1] or domain}>"
    if in_reply_to:
        message["In-Reply-To"] = in_reply_to
    if references:
        message["References"] = references
    if text and html:
        message.set_content(text)
        message.add_alternative(html, subtype="html")
    elif html:
        message.set_content(html, subtype="html")
    else:
        message.set_content(text)
    for filename, data in attachments:
        content_type = mimetypes.guess_type(filename)[0] or "application/octet-stream"
        maintype, subtype = content_type.split("/", 1)
        message.add_attachment(data, maintype=maintype, subtype=subtype, filename=filename)
    return message
