"""CloudWatch alarm email delivery: SNS -> this handler -> SES.

The template's alarms (dead-letter queues, worker/renderer/backup errors)
point ``AlarmActions`` at one SNS topic whose subscription is this
function. Every other failure path in dapier emails the operator through
``engine.notify``; alarms were the last silent failure surface — they
fired into the void because they had no actions at all.

The recipient is the same operator inbox ``notify.operator_recipient``
resolves, sent From the verified ``EmailSender`` identity, so a fired
alarm lands wherever run failures already land. The SNS message is a JSON
CloudWatch notification; anything unparseable is quoted verbatim so the
email is still actionable.
"""

import json


def handler(event, context=None):
    from .engine.notify import operator_recipient, operator_sender, ses_client

    sender = operator_sender()
    recipient = operator_recipient()
    body, subject = _summarize(event, recipient)
    ses_client().send_email(
        Source=sender,
        Destination={"ToAddresses": [recipient]},
        Message={
            "Subject": {"Data": subject, "Charset": "utf-8"},
            "Body": {"Text": {"Data": body, "Charset": "utf-8"}},
        },
    )
    return {"notified": recipient}


def _summarize(event, sender):
    """Subject and body for one SNS record; extra records append."""
    subjects, bodies = [], []
    for record in event.get("Records") or [{}]:
        raw = record.get("Sns", {}).get("Message", "")
        try:
            message = json.loads(raw)
            alarm = message.get("AlarmName", "unknown-alarm")
            reason = message.get("NewStateReason", raw)
        except (ValueError, AttributeError):
            alarm, reason = "unknown-alarm", raw
        subjects.append(f"[dapier] alarm: {alarm}")
        bodies.append(
            "A dapier CloudWatch alarm changed state.\n"
            f"\nalarm: {alarm}\n\n{str(reason)[:2000]}\n"
        )
    subject = subjects[0] if len(subjects) == 1 else f"[dapier] {len(subjects)} alarms fired"
    return "\n".join(bodies), subject
