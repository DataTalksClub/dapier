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


def data_from_message(message, pointer, limit=BODY_LIMIT):
    """The event data dict for a parsed message and its s3 pointer — the
    shape the intake envelope and the replay paths both carry."""
    return {
        "from": message.get("From"),
        "to": message.get("To", ""),
        "subject": message.get("Subject"),
        "message_id": message.get("Message-ID"),
        "text": _body(message, "plain", limit=limit),
        "html": _body(message, "html", limit=limit),
        "s3": pointer,
    }


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


def _legacy_contract(bucket, key, message):
    """The relay inbound-email v1 contract for a raw object in the legacy
    datamailer raw bucket. The worker's ingress normalizer rehydrates the
    body and stages the attachments from the raw_mime pointer exactly as it
    does for Relay's own events, so both ingestion paths share one contract
    and flows key on the same route label either way."""
    to = message.get("To", "")
    sender = message.get("From", "")
    return {
        "contract": "inbound-email",
        "version": 1,
        "event_id": f"email:{message.get('Message-ID') or key}",
        "occurred_at": message.get("Date"),
        "route": str(to or ""),
        "message_id": message.get("Message-ID"),
        "sender": {"header": str(sender or ""), "addresses": _addresses(sender)},
        "recipients": {"addresses": _addresses(to)},
        "subject": message.get("Subject"),
        "raw_mime": {"bucket": bucket, "key": key},
    }


def handler(event, _context):
    for record in event.get("Records", []):
        bucket = record["s3"]["bucket"]["name"]
        key = record["s3"]["object"]["key"]
        raw = s3.get_object(Bucket=bucket, Key=key)["Body"].read()
        message = parse_message(raw)
        if os.environ.get("LEGACY_INBOUND_BUCKET") == bucket:
            envelope = _legacy_contract(bucket, key, message)
            queue.send_message(QueueUrl=os.environ["EVENT_QUEUE_URL"],
                               MessageBody=json.dumps(envelope))
            continue
        pointer = {"bucket": bucket, "key": key}
        envelope = {
            "id": f"email:{message.get('Message-ID', key)}",
            "connector": "email",
            "event": "message.received",
            "source": message.get("To", ""),
            "occurred_at": message.get("Date"),
            "data": data_from_message(message, pointer),
        }
        queue.send_message(QueueUrl=os.environ["EVENT_QUEUE_URL"], MessageBody=json.dumps(envelope))
