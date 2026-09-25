"""Failure notifications: one email per failed run to the workflow's notify list.

A workflow opts in with a top-level ``notify:`` list of email addresses
(bundled YAML or a published designer workflow). The engine tags action
exceptions with the failing workflow's id, and the worker calls
``notify_failure`` from its error path. Delivery is at-most-once per run:
a conditional put of a ``failure-notice`` item into the executions table
claims the send, so SQS redeliveries of the failed record never re-notify.
Notice items carry their own run id (``<run>#notice``) so run history
never groups them into the real run.
"""
import os
import time
from datetime import datetime, timezone


def _ses():
    import boto3

    return boto3.client("ses")


def notify_addresses(workflow_id):
    """The workflow's notify email addresses, or [] when it opts out."""
    from .matching import all_workflows

    workflow = next((item for item in all_workflows() if item.get("id") == workflow_id), None)
    if not workflow:
        return []
    notify = workflow.get("notify")
    if isinstance(notify, str):
        notify = [notify]
    if not isinstance(notify, list):
        return []
    return [str(address).strip() for address in notify if str(address).strip()]


def _claim_notice(workflow_id, run_id, event, error):
    """Claim the one notification for this run; False when already claimed."""
    import boto3
    from botocore.exceptions import ClientError

    now = int(time.time())
    try:
        boto3.resource("dynamodb").Table(os.environ["EXECUTIONS_TABLE"]).put_item(
            Item={
                "execution_id": f"{workflow_id}:failure-notice:{event.get('id', '')}",
                "run_id": f"{run_id}#notice",
                "kind": "failure-notice",
                "workflow_id": workflow_id,
                "status": "notified",
                "error": str(error)[:500],
                "notified_at": datetime.now(timezone.utc).isoformat(),
                "expires_at": now + 90 * 86400,
            },
            ConditionExpression="attribute_not_exists(execution_id)",
        )
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
            return False
        raise
    return True


def notify_failure(exc, event, *, ses=None):
    """Email a failed run to the workflow's notify list, once per run.

    Returns a small dict describing the send, or None when the failing
    workflow is unknown, the workflow opted out, or this attempt lost the
    once-per-run claim (an SQS redelivery of the failed record).
    """
    if not isinstance(event, dict):
        return None
    workflow_id = getattr(exc, "dapier_workflow", None)
    if not workflow_id:
        return None
    addresses = notify_addresses(workflow_id)
    if not addresses:
        return None
    run_id = f"{workflow_id}:{event.get('id', '')}"
    if not _claim_notice(workflow_id, run_id, event, exc):
        return None
    if ses is None:
        ses = _ses()
    from ..triggers.email_triggers import trigger_domain

    sender = os.environ.get("DAPIER_EMAIL_SENDER") or f"no-reply@{trigger_domain()}"
    error = str(exc) or exc.__class__.__name__
    response = ses.send_email(
        Source=sender,
        Destination={"ToAddresses": addresses},
        Message={
            "Subject": {"Data": f"[dapier] Run failed: {workflow_id}", "Charset": "utf-8"},
            "Body": {"Text": {"Data": "\n".join([
                "A dapier run failed.",
                "",
                f"workflow: {workflow_id}",
                f"run: {run_id}",
                f"trigger: {event.get('connector')} / {event.get('event')}",
                "",
                f"failing step error: {error}",
            ]) + "\n", "Charset": "utf-8"}},
        },
    )
    return {"to": addresses, "run_id": run_id, "message_id": response.get("MessageId")}
