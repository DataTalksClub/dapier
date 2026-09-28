"""gmail_send action: send an email through the connection's Gmail.

Gmail's users.messages.send takes the whole message as one base64url raw
MIME document — there is no structured to/subject variant — so the send
builds an ``email.message.EmailMessage`` with the shared raw-MIME builder
(``engine.actions.email._raw_message``: text/HTML parts, the extra
recipients, and the explicit ``Message-ID`` the inbound side dedupes on).
The From address is the connection's own: Gmail only delivers from the
authenticated user (or a verified alias), so the send resolves it from
users/me/profile instead of trusting a field. The connection's OAuth grant
must include ``gmail.send``; a read-only token fails with the API's 403.
"""
import base64
import json

from ...connections import tokens
from . import base
from .email import _addresses, _raw_message
from .templating import render

GMAIL_API_URL = "https://gmail.googleapis.com/gmail/v1"
PROFILE_URL = GMAIL_API_URL + "/users/me/profile"
SEND_URL = GMAIL_API_URL + "/users/me/messages/send"


def _gmail_connection(connection_id):
    return base._connected_connection(connection_id)


def _gmail_call(method, url, access_token, payload=None, *, transport=None):
    """One Gmail REST call: parsed JSON, or a RuntimeError naming the API's
    message — the same dialect as the other engine actions' provider calls."""
    transport = transport or base._default_transport
    headers = {"authorization": f"Bearer {access_token}"}
    body = None
    if payload is not None:
        headers["content-type"] = "application/json"
        body = json.dumps(payload).encode()
    try:
        status, response = transport(method, url, headers=headers, body=body, timeout=15)
    except Exception as exc:
        raise RuntimeError(f"gmail call unreachable: {type(exc).__name__}") from None
    if status >= 300:
        detail = ""
        try:
            error = json.loads(response.decode() or "{}").get("error")
            if isinstance(error, dict) and error.get("message"):
                detail = f" ({str(error['message'])[:200]})"
        except (ValueError, UnicodeDecodeError, AttributeError):
            pass
        raise RuntimeError(f"gmail {method} returned HTTP {status}{detail}")
    try:
        return json.loads(response.decode() or "{}")
    except (ValueError, UnicodeDecodeError):
        return {}


def run_gmail_send(action, event, *, transport=None, steps=None):
    """Send an email from the connection's Gmail mailbox (users.messages.send).

    ``to``, ``cc`` and ``bcc`` each take one address or a comma-separated
    string, templated like every field; the body is the text body, the HTML
    body, or both. Output: ``{message_id, thread_id, from, to, subject}``
    plus ``cc``/``bcc`` when set — Gmail's own id for the sent message, so
    replies can thread on it later.
    """
    connection = _gmail_connection(action["connection_id"])
    access_token, _info = tokens.get_access_token(connection, transport=transport)
    to = render(action.get("to") or "", event, steps)
    addresses = [address.strip() for address in to.split(",") if address.strip()]
    if not addresses:
        raise ValueError("gmail_send needs a to address (literal or a {field} template)")
    text = render(action.get("text") or "", event, steps)
    html = render(action.get("html") or "", event, steps)
    if not text and not html:
        raise ValueError("gmail_send needs text or html")
    subject = render(action.get("subject") or "(no subject)", event, steps)
    cc = _addresses(action.get("cc"), event, steps)
    bcc = _addresses(action.get("bcc"), event, steps)

    profile = _gmail_call("GET", PROFILE_URL, access_token, transport=transport)
    sender = str(profile.get("emailAddress") or "").strip()
    if not sender:
        raise RuntimeError("gmail_send could not resolve the connection's "
                           "address: users/me/profile returned no emailAddress")
    message = _raw_message(sender, addresses, cc, bcc, [], subject, text, html,
                           [], "")
    raw = base64.urlsafe_b64encode(message.as_bytes()).decode("ascii")
    sent = _gmail_call("POST", SEND_URL, access_token, {"raw": raw},
                       transport=transport)
    output = {"message_id": sent.get("id"), "thread_id": sent.get("threadId"),
              "from": sender, "to": addresses, "subject": subject}
    if cc:
        output["cc"] = cc
    if bcc:
        output["bcc"] = bcc
    return output
