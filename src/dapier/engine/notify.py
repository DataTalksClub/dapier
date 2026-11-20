"""Failure notifications: one email per failed run, on by default.

Every failure notice goes to the workflow's top-level ``notify:`` list
(managed definition or published designer workflow) *plus* the operator
inboxes — ``notify.operator_recipients()``: ``DAPIER_NOTIFY_EMAIL``
(template.yaml ``NotifyEmail``) and ``DAPIER_NOTIFY_EXTRA_EMAILS``
(template.yaml ``NotifyExtraEmails``, the human operator's inbox). An
explicit ``notify: []`` opts a workflow out entirely. Failures with no
workflow behind them (a poll trigger that could not fetch) notify the
operator inboxes too: the worker hands ``notify_failure`` a minimal
envelope naming the trigger.

The engine tags action exceptions with the failing workflow's id, and the
worker calls ``notify_failure`` from its error path. Delivery is
at-most-once per run: a conditional put of a ``failure-notice`` item into
the executions table claims the send, so redeliveries of the failed record
never re-notify. A failed SES call releases the claim so a later attempt
can still deliver. Notice items carry their own run id (``<run>#notice``)
so run history never groups them into the real run.

The auto-pause trip wire (engine.worker) sends its own notice through the
same path once the streak pauses a workflow — :func:`notify_auto_pause`,
same recipients, its own claim kind.
"""
import logging
import os
import time
from datetime import datetime, timezone


logger = logging.getLogger(__name__)


def ses_client():
    """SES client in the region where EmailSender is verified."""
    import boto3

    region = str(os.environ.get("DAPIER_EMAIL_REGION") or "").strip() or None
    return boto3.client("ses", region_name=region)


def operator_sender():
    """The SES-verified From address for operator mail.

    Prefers ``DAPIER_ALERT_SENDER`` (template.yaml ``AlertEmailSender``),
    falling back to ``DAPIER_EMAIL_SENDER`` (template.yaml ``EmailSender``),
    then the trigger domain's no-reply identity. This is the From header,
    not the inbox that reads failures.
    """
    sender = str(os.environ.get("DAPIER_ALERT_SENDER") or os.environ.get("DAPIER_EMAIL_SENDER") or "").strip()
    if sender:
        return sender
    from ..triggers.email_triggers import trigger_domain

    return f"no-reply@{trigger_domain()}"


def operator_recipient():
    """The primary inbox that reads failure mail.

    Prefers ``DAPIER_NOTIFY_EMAIL`` (template.yaml ``NotifyEmail``), then
    ``BACKUP_ALERT_EMAIL`` (the same operator inbox backup failures use),
    then the verified sender. Sending To the From address is silent: the
    trigger domain's no-reply identity is not a mailbox anyone reads.
    See :func:`operator_recipients` for the full list operator mail goes to.
    """
    for name in ("DAPIER_NOTIFY_EMAIL", "BACKUP_ALERT_EMAIL"):
        address = str(os.environ.get(name) or "").strip()
        if address:
            return address
    from email.utils import parseaddr

    sender = operator_sender()
    return parseaddr(sender)[1] or sender


def operator_recipients():
    """Every inbox operator mail is delivered to, primary first.

    The primary address from :func:`operator_recipient` plus the extra
    operator inboxes in ``DAPIER_NOTIFY_EXTRA_EMAILS`` (template.yaml
    ``NotifyExtraEmails``, comma-separated). Deduplicated, order stable.
    A human operator must stay on this list: routing failure mail only
    to a bot mailbox left the 2026-11 crosspost outage invisible to the
    person who needed to know.
    """
    addresses = [operator_recipient()]
    for token in str(os.environ.get("DAPIER_NOTIFY_EXTRA_EMAILS") or "").replace(";", ",").split(","):
        address = token.strip()
        if address and address not in addresses:
            addresses.append(address)
    return addresses


def _console_runs_url():
    base = str(os.environ.get("HOOKS_BASE_URL") or "").rstrip("/")
    return f"{base}/runs" if base else ""


def notify_addresses(workflow_id):
    """The email addresses a failure notice for this workflow goes to.

    The workflow's ``notify`` list is always included. An explicit
    ``notify: []`` opts out entirely. When the workflow has no ``notify:``
    key (or a malformed value) the operator inboxes are the recipients;
    with an explicit list they join it, so failure mail keeps reaching
    the operator inboxes regardless of what a workflow adds.
    """
    from .matching import all_workflows

    workflow = next((item for item in all_workflows() if item.get("id") == workflow_id), None)
    if not workflow:
        return []
    notify = workflow.get("notify")
    if notify is None or not isinstance(notify, (str, list)):
        return operator_recipients()
    if isinstance(notify, str):
        notify = [notify]
    addresses = [str(address).strip() for address in notify if str(address).strip()]
    if not addresses:
        return []  # an explicit ``notify: []`` opts out entirely
    for address in operator_recipients():
        if address not in addresses:
            addresses.append(address)
    return addresses


def _ingress_subject(event):
    """A name for a failure with no workflow behind it, or None.

    Only named ingress failures notify the operator — poll-trigger fires the
    worker could not run (the connector's ``poll`` marker names the trigger)
    and Zoom events that matched no workflow. Arbitrary untagged queue
    records stay quiet: the DLQ and worker alarms cover garbage volume.
    """
    data = event.get("data") if isinstance(event.get("data"), dict) else {}
    poll = event.get("poll") or data.get("poll")
    if event.get("connector") == "poll" and poll:
        return f"poll:{poll}"
    if event.get("connector") == "zoom":
        return "zoom-unmatched"
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


def _release_notice(subject, event, kind="failure-notice"):
    """Drop a claim so a later attempt can send after a failed SES call."""
    import boto3

    try:
        boto3.resource("dynamodb").Table(os.environ["EXECUTIONS_TABLE"]).delete_item(
            Key={"execution_id": f"{subject}:{kind}:{event.get('id', '')}"},
        )
    except Exception:
        logger.exception("could not release failure-notice claim",
                         extra={"workflow_id": subject, "kind": kind})


def _send(subject, run_id, addresses, lines, *, ses=None):
    """One SES send with the notice house style; returns the send summary."""
    if ses is None:
        ses = ses_client()
    response = ses.send_email(
        Source=operator_sender(),
        Destination={"ToAddresses": addresses},
        Message={
            "Subject": {"Data": lines["subject"], "Charset": "utf-8"},
            "Body": {"Text": {"Data": "\n".join(lines["body"]) + "\n",
                              "Charset": "utf-8"}},
        },
    )
    return {"to": addresses, "run_id": run_id, "message_id": response.get("MessageId")}


def _notice_body(kind_line, subject, run_id, event, error, extra=None):
    """Plain-text notice lines, including a console link when configured."""
    lines = [
        kind_line,
        "",
        f"workflow: {subject}",
        f"run: {run_id}",
        f"trigger: {event.get('connector')} / {event.get('event')}",
        "",
        f"failing step error: {error}",
    ]
    console = _console_runs_url()
    if console:
        lines.extend(["", f"console: {console}"])
    if extra:
        lines.extend(["", *extra])
    return lines


def notify_failure(exc, event, *, ses=None):
    """Email a failed run to its notify list, once per run.

    Recipients: the failing workflow's ``notify`` list plus the operator
    inboxes (an explicit ``notify: []`` opts the workflow out); a failure
    with no workflow behind it — the worker passes a minimal poll
    envelope — goes to the operator inboxes. Returns a small dict
    describing the send, or None when the event is not a dict, no
    recipient list resolves, or this attempt lost the once-per-run claim
    (a redelivery of the same failure).
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
        addresses = operator_recipients()
    if not addresses:
        return None
    run_id = f"{subject}:{event.get('id', '')}"
    if not _claim_notice(subject, run_id, event, exc):
        return None
    error = str(exc) or exc.__class__.__name__
    try:
        return _send(subject, run_id, addresses, {
            "subject": f"[dapier] Run failed: {subject}",
            "body": _notice_body("A dapier run failed.", subject, run_id, event, error),
        }, ses=ses)
    except Exception:
        logger.exception("failure notice email could not be sent",
                         extra={"workflow_id": subject, "run_id": run_id})
        _release_notice(subject, event)
        return None


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
    extra = [
        "The workflow is paused and will not run again until it is "
        "re-enabled (CLI: `dapier workflows on <file>.yaml`, or the "
        "console toggle). Re-enabling clears the pause.",
    ]
    try:
        return _send(workflow_id, run_id, addresses, {
            "subject": f"[dapier] Workflow auto-paused: {workflow_id}",
            "body": _notice_body(
                "A dapier workflow was paused after repeated failures.",
                workflow_id, run_id, event, error, extra=extra),
        }, ses=ses)
    except Exception:
        logger.exception("auto-pause notice email could not be sent",
                         extra={"workflow_id": workflow_id, "run_id": run_id})
        _release_notice(workflow_id, event, kind="auto-pause-notice")
        return None