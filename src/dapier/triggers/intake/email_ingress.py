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


def handler(event, _context):
    for record in event.get("Records", []):
        bucket = record["s3"]["bucket"]["name"]
        key = record["s3"]["object"]["key"]
        raw = s3.get_object(Bucket=bucket, Key=key)["Body"].read()
        message = BytesParser(policy=policy.default).parsebytes(raw)
        envelope = {
            "id": f"email:{message.get('Message-ID', key)}",
            "connector": "email",
            "event": "message.received",
            "source": message.get("To", ""),
            "occurred_at": message.get("Date"),
            "data": {
                "from": message.get("From"),
                "to": message.get("To"),
                "subject": message.get("Subject"),
                "message_id": message.get("Message-ID"),
                "text": _body(message, "plain"),
                "html": _body(message, "html"),
                "s3": {"bucket": bucket, "key": key},
            },
        }
        queue.send_message(QueueUrl=os.environ["EVENT_QUEUE_URL"], MessageBody=json.dumps(envelope))
