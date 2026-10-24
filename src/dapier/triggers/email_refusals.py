"""Operator signal for sender-policy drops at enabled workflow mailboxes.

The worker never sends a refusal email. A shared CloudWatch alarm consumes
the metric; atomic cursor claims limit it to one datapoint per address and
sender per 24 hours, across workers and queue redeliveries. Dapier's own
operator notices are logged but never feed the alarm back into itself.
"""

import hashlib
import json
import logging
import os
import time

from . import email_from, email_triggers

logger = logging.getLogger(__name__)
WINDOW_SECONDS = 86400
METRIC = "EmailSenderPolicyRefused"


def _operator_senders():
    from ..engine.notify import operator_sender
    from .email_triggers import trigger_domain

    domain = (trigger_domain() or "dtcdev.click").lower()
    candidates = {
        operator_sender(),
        os.environ.get("DAPIER_ALERT_SENDER"),
        os.environ.get("DAPIER_EMAIL_SENDER"),
        f"alerts@{domain}",
        f"no-reply@{domain}",
        "alerts@dtcdev.click",
        "no-reply@dtcdev.click",
    }
    return {email_from.bare_address(s) for s in candidates if s}


def _operator_notice(event, senders):
    subject = str((event.get("data") or {}).get("subject") or "").strip()
    return bool(_operator_senders().intersection(senders)) and subject.startswith((
        "[dapier]", "dapier error digest:",
    ))


def signal(event, *, table_ref=None, cloudwatch=None, now=None):
    """Log a refused delivery and emit a deduped metric for an active mailbox.

    Called only after the sender gate refuses an inbound message. Mailbox
    ownership uses the same route inventory as the console/CLI, regardless
    of other trigger filters on the message. Storage/metric failures raise
    so the worker retries instead of silently losing the operator signal.
    """
    from botocore.exceptions import ClientError
    import boto3

    route = email_from.route_name(event)
    owners = sorted({row["workflow"] for row in email_triggers.managed_routes()
                     if row["name"] == route and row["status"] == "enabled"})
    if not owners:
        return 0
    address = email_triggers.address_for(route)
    senders = email_from.sender_addresses(event)
    logger.error("inbound email refused: sender not allowed; address=%s sender=%s workflows=%s",
                 address, ",".join(senders) or "unknown", ",".join(owners))
    if _operator_notice(event, senders):
        return 0

    table = table_ref if table_ref is not None else boto3.resource("dynamodb").Table(
        os.environ["CURSORS_TABLE"])
    cloudwatch = cloudwatch if cloudwatch is not None else boto3.client("cloudwatch")
    now = int(time.time()) if now is None else int(now)
    emitted = 0
    for sender in senders or ["unknown"]:
        pair = hashlib.sha256(json.dumps([address, sender]).encode()).hexdigest()
        key = {"cursor_id": f"email-refusal#{pair}"}
        expires = now + WINDOW_SECONDS
        try:
            table.put_item(
                Item={**key, "expires_at": expires},
                ConditionExpression="attribute_not_exists(cursor_id) OR expires_at <= :now",
                ExpressionAttributeValues={":now": now},
            )
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
                continue
            raise
        try:
            cloudwatch.put_metric_data(
                Namespace="Dapier/Triggers",
                MetricData=[{"MetricName": METRIC, "Value": 1, "Unit": "Count"}],
            )
        except Exception:
            # Failed emission must not suppress a later delivery's retry.
            table.delete_item(Key=key, ConditionExpression="expires_at = :expires",
                              ExpressionAttributeValues={":expires": expires})
            raise
        emitted += 1
    return emitted
