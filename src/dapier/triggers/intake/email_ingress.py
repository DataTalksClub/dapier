import json
import os
from email import policy
from email.parser import BytesParser
from email.utils import getaddresses

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


def local_part(to_header):
    """The first recipient's local-part, lowercased, or ``""``."""
    parsed = getaddresses([to_header or ""])
    if not parsed:
        return ""
    address = (parsed[0][1] or "").strip().lower()
    if "@" not in address:
        return ""
    return address.split("@", 1)[0]


def _agent_mailbox_enabled(name):
    """True only for a stored, enabled agent mailbox. A missing row is not one."""
    if not name or not os.environ.get("AGENT_MAILBOXES_TABLE"):
        return False
    from ..agent_mailboxes import peek_mailbox

    item = peek_mailbox(name)
    return bool(item and item.get("enabled", True))


def handler(event, _context):
    for record in event.get("Records", []):
        bucket = record["s3"]["bucket"]["name"]
        key = record["s3"]["object"]["key"]
        raw = s3.get_object(Bucket=bucket, Key=key)["Body"].read()
        message = BytesParser(policy=policy.default).parsebytes(raw)
        to_header = message.get("To", "")
        pointer = {"bucket": bucket, "key": key}
        if _agent_mailbox_enabled(local_part(to_header)):
            body = {
                "mailbox": local_part(to_header),
                "message_id": message.get("Message-ID") or key,
                "from": message.get("From"),
                "to": to_header,
                "subject": message.get("Subject"),
                "date": message.get("Date"),
                "s3": pointer,
            }
            queue.send_message(
                QueueUrl=os.environ["AGENT_TASK_QUEUE_URL"],
                MessageBody=json.dumps(body),
            )
            continue
        envelope = {
            "id": f"email:{message.get('Message-ID', key)}",
            "connector": "email",
            "event": "message.received",
            "source": to_header,
            "occurred_at": message.get("Date"),
            "data": {
                "from": message.get("From"),
                "to": to_header,
                "subject": message.get("Subject"),
                "message_id": message.get("Message-ID"),
                "text": _body(message, "plain"),
                "html": _body(message, "html"),
                "s3": pointer,
            },
        }
        queue.send_message(QueueUrl=os.environ["EVENT_QUEUE_URL"], MessageBody=json.dumps(envelope))
