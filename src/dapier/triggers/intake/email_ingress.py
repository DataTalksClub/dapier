import json
import os
from email import policy
from email.parser import BytesParser
import boto3


s3 = boto3.client("s3")
queue = boto3.client("sqs")

# The decoded body rides the event so workflows can read what the message
# said; capped per part so an ordinary message stays under the trigger-input
# replay limit (engine.worker.TRIGGER_INPUT_LIMIT) instead of being stored
# as a truncated, unreplayable preview.
BODY_LIMIT = 60_000

# The reading pane is not the replay path: it may show more than the engine
# ever delivered. Capped well under the Lambda 6 MB response so one lavish
# newsletter cannot dominate the detail payload.
DISPLAY_BODY_LIMIT = 500_000

# The worker stages rehydrated attachment parts into the mail bucket under
# this prefix (connectors.ingress._stage_attachments). Those objects are
# message parts, not messages: the bucket's ObjectCreated trigger must skip
# them, or every staged attachment re-enters the intake as a header-less
# "email" that the sender allow-list then refuses (and an attached .eml
# would run a phantom second delivery).
STAGED_ATTACHMENT_PREFIX = "relay-attachments/"


def _body(message, *prefer, limit=BODY_LIMIT):
    """One decoded body part (``"plain"`` or ``"html"``), capped, or None."""
    part = message.get_body(preferencelist=prefer)
    if part is None:
        return None
    try:
        content = part.get_content()
    except (LookupError, UnicodeDecodeError):
        return None  # undecodable charset — the s3 pointer still has the raw
    return content[:limit] if isinstance(content, str) else None


def parse_message(raw):
    return BytesParser(policy=policy.default).parsebytes(raw)


def _route_label(route):
    name = str(route or "").strip().lower()
    if "@" in name:
        name = name.split("@", 1)[0].strip()
    return name


def data_from_message(message, pointer, limit=BODY_LIMIT):
    """The event data dict for a parsed message and its s3 pointer — the
    shape the intake envelope and the replay paths both carry."""
    to = message.get("To", "")
    return {
        "from": message.get("From"),
        "to": to,
        "route": _route_label(to),
        "subject": message.get("Subject"),
        "message_id": message.get("Message-ID"),
        "text": _body(message, "plain", limit=limit),
        "html": _body(message, "html", limit=limit),
        "s3": pointer,
        **({"authentication_results": [str(value)[:8192] for value in message.get_all("Authentication-Results", [])[:10]]}
           if message.get_all("Authentication-Results") else {}),
        **({"dkim_signatures": [str(value)[:8192] for value in message.get_all("DKIM-Signature", [])[:10]]}
           if message.get_all("DKIM-Signature") else {}),
        **({"received_spf": [str(value)[:8192] for value in message.get_all("Received-SPF", [])[:10]]}
           if message.get_all("Received-SPF") else {}),
    }


def contract_fields(data, event_id):
    """The inbound-email v1 contract fields for a rebuilt email event.

    A replay rebuilds the flat intake data, but routed flows key on what
    the intake resolves around it: the route label, the sender and
    recipient shapes, and attachments staged from the raw copy at delivery
    time. Merging these fields into a replay envelope makes the worker's
    email normalizer re-run on it exactly as on a live delivery — the
    worker rehydrates the body and stages the attachments from the
    ``raw_mime`` copy — while ``data`` stays for the sample and reading
    views.
    """
    to = str(data.get("to") or "")
    sender = str(data.get("from") or "")
    # A record small enough to store whole keeps the normalized event shape
    # (sender/recipients objects, bare route label) instead of the flat
    # intake data, so the sender/recipient/route reads fall back to that
    # shape - without it a legacy replay carries an empty sender and the
    # route's allow-list refuses it (the 2026-10-16 newsletter case).
    sender_obj = data.get("sender") if isinstance(data.get("sender"), dict) else {}
    recipients_obj = (
        data.get("recipients") if isinstance(data.get("recipients"), dict) else {})
    if not sender:
        sender = str(sender_obj.get("header") or "")
    if not to:
        addresses = recipients_obj.get("addresses") or []
        to = str(recipients_obj.get("to") or (addresses[0] if addresses else ""))
    if not to:
        to = str(data.get("route") or "")
    fields = {
        "contract": "inbound-email",
        "version": 1,
        "event_id": event_id,
        "route": to,
        "message_id": data.get("message_id"),
        "sender": {"header": sender, "addresses": _addresses(sender)},
        "recipients": {"addresses": _addresses(to)},
        "subject": data.get("subject"),
        # The email's own Date header when the record kept it, so a replayed
        # event reports the original receipt time rather than the replay
        # moment. Flat rebuilds have no date and fall back to occurred_at
        # in the worker's normalizer.
        "date": data.get("date"),
    }
    pointer = data.get("s3") or data.get("raw_mime")
    if isinstance(pointer, dict) and pointer.get("bucket") and pointer.get("key"):
        fields["raw_mime"] = {"bucket": pointer["bucket"], "key": pointer["key"]}
    return fields


def replay_data(pointer, limit=BODY_LIMIT):
    """Rebuild stored email event data from its raw s3 copy, for replay.

    Same caps as the intake envelope by default, so a replayed event
    carries exactly what the engine first delivered (the flow never saw
    more either). Pass ``limit=DISPLAY_BODY_LIMIT`` for the reading pane,
    which may show more than the replay path carries.
    Returns ``(data, None)``, or ``(None, error)`` when the raw copy cannot
    be fetched or parsed.
    """
    if not isinstance(pointer, dict) or not pointer.get("bucket") or not pointer.get("key"):
        return None, "no usable s3 pointer"
    try:
        raw = s3.get_object(Bucket=pointer["bucket"], Key=pointer["key"])["Body"].read()
    except Exception:
        return None, "the raw email could not be fetched from s3"
    try:
        message = parse_message(raw)
    except Exception:
        return None, "the raw email could not be parsed"
    return data_from_message(
        message, {"bucket": pointer["bucket"], "key": pointer["key"]},
        limit=limit), None


def _addresses(header):
    """Bare addresses from a header that may carry display names or lists."""
    from email.utils import getaddresses

    return [address for _name, address in getaddresses([str(header or "")]) if address]


def handler(event, _context):
    for record in event.get("Records", []):
        bucket = record["s3"]["bucket"]["name"]
        key = record["s3"]["object"]["key"]
        if key.startswith(STAGED_ATTACHMENT_PREFIX):
            continue
        raw = s3.get_object(Bucket=bucket, Key=key)["Body"].read()
        message = parse_message(raw)
        pointer = {"bucket": bucket, "key": key}
        to = message.get("To", "")
        envelope = {
            "id": f"email:{message.get('Message-ID', key)}",
            "connector": "email",
            "event": "message.received",
            "source": _route_label(to),
            "occurred_at": message.get("Date"),
            "data": data_from_message(message, pointer),
        }
        queue.send_message(QueueUrl=os.environ["EVENT_QUEUE_URL"], MessageBody=json.dumps(envelope))
