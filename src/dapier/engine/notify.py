"""Failure notifications: one email per failed run, on by default.

A workflow's top-level ``notify:`` list of email addresses (managed definition or
a published designer workflow) picks its recipients. With no ``notify:``
key the operator address (DAPIER_EMAIL_SENDER) is notified instead — error
visibility is the default — and an explicit ``notify: []`` opts out.
Failures with no workflow behind them (a poll trigger that could not
fetch) notify the operator too: the worker hands ``notify_failure`` a
minimal envelope naming the trigger.

The engine tags action exceptions with the failing workflow's id, and the
worker calls ``notify_failure`` from its error path. Delivery is
at-most-once per run: a conditional put of a ``failure-notice`` item into
the executions table claims the send, so redeliveries of the failed record
never re-notify. Notice items carry their own run id (``<run>#notice``) so
run history never groups them into the real run.

The auto-pause trip wire (engine.worker) sends its own notice through the
same path once the streak pauses a workflow — :func:`notify_auto_pause`,
same recipients, its own claim kind.
"""
import os
import time
from datetime import datetime, timezone


def _ses():
    import boto3

    return boto3.client("ses")


def operator_recipient():
    """The default failure recipient: the configured sender address.

    DAPIER_EMAIL_SENDER is the SES-verified from address (template.yaml
    ``EmailSender``); with it unset, the trigger domain's no-reply plays
    the role, matching the send path's fallback below.
    """
    sender = str(os.environ.get("DAPIER_EMAIL_SENDER") or "").strip()
    if sender:
        return sender
    from ..triggers.email_triggers import trigger_domain

    return f"no-reply@{trigger_domain()}"


def notify_addresses(workflow_id):
    """The workflow's notify email addresses.

    An explicit ``notify: []`` opts out; a workflow with no ``notify:`` key
    (or a malformed value) defaults to the operator address.
    """
    from .matching import all_workflows

    workflow = next((item for item in all_workflows() if item.get("id") == workflow_id), None)
    if not workflow:
        return []
    notify = workflow.get("notify")
    if isinstance(notify, str):
        notify = [notify]
    if not isinstance(notify, list):
        notify = [operator_recipient()]
    return [str(address).strip() for address in notify if str(address).strip()]


def _ingress_subject(event):
    """A name for a failure with no workflow behind it, or None.

    Only named ingress failures notify the operator — today, poll-trigger
    fires the worker could not run (the connector's ``poll`` marker names
    the trigger). Arbitrary untagged queue records stay quiet: the DLQ and
    worker alarms cover garbage volume.
    """
    data = event.get("data") if isinstance(event.get("data"), dict) else {}
    poll = event.get("poll") or data.get("poll")
    if event.get("connector") == "poll" and poll:
        return f"poll:{poll}"
    return None


def _claim_notice(subject, run_id, event, error, kind="failure-notice"):
    """Claim the one notification for this run; False when already claimed."""
    import boto3
    from botocore.exceptions import ClientError

    now = int(time.time())
    try:
        boto3.resource("dynamodb").Table(os.environ["EXECUTIONS_TABLE"]).put_item(
            Item={
                "execution_id": f"{subject}:{kind}:{event.get('id', '')}",
                "run_id": f"{run_id}#notice",
                "kind": kind,
                "workflow_id": subject,
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


def _send(subject, run_id, addresses, lines, *, ses=None):
    """One SES send with the notice house style; returns the send summary."""
    if ses is None:
        ses = _ses()
    from ..triggers.email_triggers import trigger_domain

    sender = os.environ.get("DAPIER_EMAIL_SENDER") or f"no-reply@{trigger_domain()}"
    response = ses.send_email(
        Source=sender,
        Destination={"ToAddresses": addresses},
        Message={
            "Subject": {"Data": lines["subject"], "Charset": "utf-8"},
            "Body": {"Text": {"Data": "\n".join(lines["body"]) + "\n",
                              "Charset": "utf-8"}},
        },
    )
    return {"to": addresses, "run_id": run_id, "message_id": response.get("MessageId")}


def notify_failure(exc, event, *, ses=None):
    """Email a failed run to its notify list, once per run.

    Recipients: the failing workflow's ``notify`` list, defaulting to the
    operator address when the workflow has no ``notify:`` (an explicit
    ``notify: []`` opts out); a failure with no workflow behind it — the
    worker passes a minimal poll envelope — goes to the operator. Returns
    a small dict describing the send, or None when the event is not a
    dict, no recipient list resolves, or this attempt lost the
    once-per-run claim (a redelivery of the same failure).
    """
    if not isinstance(event, dict):
        return None
    workflow_id = getattr(exc, "dapier_workflow", None)
    if workflow_id:
        subject = workflow_id
        addresses = notify_addresses(workflow_id)
    else:
        subject = _ingress_subject(event)
        if not subject:
            return None
        addresses = [operator_recipient()]
    if not addresses:
        return None
    run_id = f"{subject}:{event.get('id', '')}"
    if not _claim_notice(subject, run_id, event, exc):
        return None
    error = str(exc) or exc.__class__.__name__
    return _send(subject, run_id, addresses, {
        "subject": f"[dapier] Run failed: {subject}",
        "body": [
            "A dapier run failed.",
            "",
            f"workflow: {subject}",
            f"run: {run_id}",
            f"trigger: {event.get('connector')} / {event.get('event')}",
            "",
            f"failing step error: {error}",
        ],
    }, ses=ses)


def notify_auto_pause(exc, event, *, ses=None):
    """Email the owner that the workflow was paused after repeated failures.

    The worker calls this right after the failure notice, and only when the
    failure actually tripped the auto-pause (engine.worker._auto_pause_on_failure
    flipped the ``auto_paused`` flag on the stored definition) — so the
    announcement goes out once per pause, not per past-threshold failure.
    Same recipients as the failure notice (the workflow's ``notify`` list,
    operator by default), its own claim kind so a redelivery can never
    re-announce. None when there is no workflow behind the failure.
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
    if not _claim_notice(workflow_id, run_id, event, exc, kind="auto-pause-notice"):
        return None
    error = str(exc) or exc.__class__.__name__
    return _send(workflow_id, run_id, addresses, {
        "subject": f"[dapier] Workflow auto-paused: {workflow_id}",
        "body": [
            "A dapier workflow was paused after repeated failures.",
            "",
            f"workflow: {workflow_id}",
            f"run: {run_id}",
            f"trigger: {event.get('connector')} / {event.get('event')}",
            "",
            f"failing step error: {error}",
            "",
            "The workflow is paused and will not run again until it is "
            "re-enabled (CLI: `dapier workflows on <file>.yaml`, or the "
            "console toggle). Re-enabling clears the pause.",
        ],
    }, ses=ses)