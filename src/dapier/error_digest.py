"""Scheduled operator error digest: the errors summary as a daily email.

ErrorDigestFunction (template.yaml) invokes handler() on a daily EventBridge
schedule. It renders the same summary the console and CLI read — api/errors.py
``api_summary``, one domain function, no second grouping — into a short
plain-text email and sends it to the same operator address
``engine.notify.operator_recipient`` resolves, mirroring alarm_notify's SES
call. A window with zero failed runs sends nothing: the digest exists to
surface new failures, not to confirm quiet days.

Send-now parity (UI/CLI rule): POST /api/{admin,agent}/errors/digest and
``dapier errors send-digest`` call ``send()`` directly and return what was
sent (or ``{"skipped": true}``).
"""
import os


def _window_days():
    """Digest window in days (DAPIER_ERROR_DIGEST_DAYS, default 1 = 24h)."""
    try:
        return max(1, int(os.environ.get("DAPIER_ERROR_DIGEST_DAYS") or 1))
    except (TypeError, ValueError):
        return 1


def render(summary):
    """Subject and plain-text body for one ``errors.api_summary`` payload.

    Short by design: one line per failing workflow (count) plus its most
    recent failure (timestamp + truncated error).
    """
    total = int(summary.get("total_failed_runs") or 0)
    days = int(summary.get("window_days") or 1)
    window = "24h" if days == 1 else f"{days} days"
    plural = "" if total == 1 else "s"
    subject = f"dapier error digest: {total} failed run{plural} (last {window})"
    lines = [f"Failed runs by workflow over the last {window}: {total} total.", ""]
    for row in summary.get("workflows") or []:
        workflow_id = row.get("workflow_id") or "unknown"
        lines.append(f"{workflow_id}: {row.get('failed_runs', 0)} failed")
        if row.get("last_failed_at"):
            error = str(row.get("last_error") or "")[:200]
            lines.append(f"  last: {row['last_failed_at']}" + (f"  {error}" if error else ""))
    return subject, "\n".join(lines) + "\n"


def send(days=None, *, ses=None):
    """Render the current errors summary and email it to the operator.

    Returns what was sent (``sent``, recipient, subject, body, counts) or
    ``{"skipped": True, ...}`` when nothing failed in the window — no SES
    call, no noise email.
    """
    from .api.errors import api_summary
    from .engine.notify import operator_recipient

    _, summary = api_summary(days if days is not None else _window_days())
    total = int(summary.get("total_failed_runs") or 0)
    if total == 0:
        return {
            "skipped": True,
            "window_days": summary.get("window_days"),
            "total_failed_runs": 0,
        }
    subject, body = render(summary)
    recipient = operator_recipient()
    if ses is None:
        import boto3

        ses = boto3.client("ses")
    response = ses.send_email(
        Source=recipient,
        Destination={"ToAddresses": [recipient]},
        Message={
            "Subject": {"Data": subject, "Charset": "utf-8"},
            "Body": {"Text": {"Data": body, "Charset": "utf-8"}},
        },
    )
    return {
        "sent": True,
        "to": recipient,
        "subject": subject,
        "body": body,
        "message_id": response.get("MessageId"),
        "window_days": summary.get("window_days"),
        "total_failed_runs": total,
    }


def handler(event, context=None):
    return send()
