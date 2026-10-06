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


def _body(message, *prefer):
    """One decoded body part (``"plain"`` or ``"html"``), capped, or None."""
    part = message.get_body(preferencelist=prefer)
    if part is None:
        return None
    try:
        content = part.get_content()
    except (LookupError, UnicodeDecodeError):
        return None  # undecodable charset — the s3 pointer still has the raw
    return content[:BODY_LIMIT] if isinstance(content, str) else None


def parse_message(raw):
    return BytesParser(policy=policy.default).parsebytes(raw)


def data_from_message(message, pointer):
    """The event data dict for a parsed message and its s3 pointer — the
    shape the intake envelope and the replay paths both carry."""
    return {
        "from": message.get("From"),
        "to": message.get("To", ""),
        "subject": message.get("Subject"),
        "message_id": message.get("Message-ID"),
        "text": _body(message, "plain"),
        "html": _body(message, "html"),
        "s3": pointer,
    }


def replay_data(pointer):
    """Rebuild stored email event data from its raw s3 copy, for replay.

    Same caps as the intake envelope, so a replayed event carries exactly
    what the engine first delivered (the flow never saw more either).
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
        message, {"bucket": pointer["bucket"], "key": pointer["key"]}), None


def handler(event, _context):
    for record in event.get("Records", []):
        bucket = record["s3"]["bucket"]["name"]
        key = record["s3"]["object"]["key"]
        raw = s3.get_object(Bucket=bucket, Key=key)["Body"].read()
        message = parse_message(raw)
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
