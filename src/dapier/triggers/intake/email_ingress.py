import json
import os
from email import policy
from email.parser import BytesParser

import boto3


s3 = boto3.client("s3")
queue = boto3.client("sqs")


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
                "s3": {"bucket": bucket, "key": key},
            },
        }
        queue.send_message(QueueUrl=os.environ["EVENT_QUEUE_URL"], MessageBody=json.dumps(envelope))
