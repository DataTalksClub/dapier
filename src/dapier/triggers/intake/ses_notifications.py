"""SES feedback intake: bounce and complaint notifications to workflow events.

SES reports bounces and complaints through SNS, which POSTs a JSON envelope
to the feedback destination (the configuration set's feedback hook,
``/hooks/ses-notifications``). Like Mailchimp, the unguessable URL is the
credential, so the topic allowlist (``SES_NOTIFICATION_TOPICS``, a
comma-separated TopicArn list) is the real gate: with it set, an envelope
naming another topic is 403 and nothing runs; operators should always set
it. A ``SubscriptionConfirmation`` is answered by GETting the ``SubscribeURL``
(best-effort — SNS retries until confirmed); a ``Notification`` whose
message is a Bounce publishes ``connector="email"``, ``event="bounce.received"``
and a Complaint ``event="complaint.received"`` — the events stored email
watchers watch (email_triggers) and the email chip's samples document
(connectors.email). Deliveries publish nothing; malformed bodies are a 400,
never a 5xx, so a provider retry loop cannot thrash the function.
"""

import json
import os
import urllib.request

BOUNCE_EVENT = "bounce.received"
COMPLAINT_EVENT = "complaint.received"

# The SES notification types this intake publishes, mapped to the trigger
# events they become; Delivery (and anything else) is accepted with no event.
NOTIFICATION_EVENTS = {"Bounce": BOUNCE_EVENT, "Complaint": COMPLAINT_EVENT}


def allowed_topics():
    """The configured TopicArn allowlist; empty means accept-any (unset)."""
    return {
        topic.strip() for topic in os.environ.get("SES_NOTIFICATION_TOPICS", "").split(",")
        if topic.strip()
    }


def _recipients(entries):
    """The email addresses from a bounced/complained-recipients list."""
    return [
        str(entry.get("emailAddress"))
        for entry in (entries or [])
        if isinstance(entry, dict) and entry.get("emailAddress")
    ]


def _data(message, event):
    """The flattened, JSON-safe payload a workflow reads: the feedback and
    mail-envelope fields both event kinds carry, plus the kind's own fields
    — the exact shape the email bounce/complaint samples document."""
    feedback = message.get("bounce") or message.get("complaint") or {}
    mail = message.get("mail") or {}
    data = {
        "feedback_type": "bounce" if event == BOUNCE_EVENT else "complaint",
        "feedback_id": feedback.get("feedbackId"),
        "message_id": mail.get("messageId"),
        "timestamp": feedback.get("timestamp") or mail.get("timestamp"),
        "source": mail.get("source"),
        "destination": list(mail.get("destination") or []),
    }
    if event == BOUNCE_EVENT:
        data["bounce_type"] = feedback.get("bounceType")
        data["bounce_subtype"] = feedback.get("bounceSubType")
        data["bounced_recipients"] = _recipients(feedback.get("bouncedRecipients"))
    else:
        data["complained_recipients"] = _recipients(feedback.get("complainedRecipients"))
    return data


def _confirm(url):
    """GET the SubscribeURL so SNS confirms the subscription; best-effort,
    since a failed GET only means SNS keeps retrying the confirmation."""
    if not url:
        return
    try:
        with urllib.request.urlopen(urllib.request.Request(url, method="GET"), timeout=10):
            pass
    except OSError:
        pass


def handle(body, *, publish):
    """One SNS delivery to ``/hooks/ses-notifications``: ``(status, response
    payload)``. Never raises for a bad body — SNS retries non-2xx, and a
    malformed POST should settle, not loop."""
    try:
        envelope = json.loads((body or b"").decode(errors="replace"))
    except ValueError:
        return 400, {"error": "body is not valid JSON"}
    if not isinstance(envelope, dict):
        return 400, {"error": "body must be a JSON object"}
    topics = allowed_topics()
    topic = str(envelope.get("TopicArn") or "")
    if topics and topic not in topics:
        return 403, {"error": "topic is not in SES_NOTIFICATION_TOPICS"}
    kind = str(envelope.get("Type") or "")
    if kind == "SubscriptionConfirmation":
        _confirm(str(envelope.get("SubscribeURL") or ""))
        return 200, {"accepted": True, "confirmed": True}
    if kind == "UnsubscribeConfirmation":
        # Never GET the UnsubscribeURL: that would unsubscribe the topic.
        return 200, {"accepted": True}
    if kind != "Notification":
        return 400, {"error": f"unsupported SNS type '{kind or '(missing)'}'"}
    try:
        message = json.loads(envelope.get("Message") or "")
    except ValueError:
        return 400, {"error": "notification Message is not valid JSON"}
    if not isinstance(message, dict):
        return 400, {"error": "notification Message must be a JSON object"}
    event = NOTIFICATION_EVENTS.get(str(message.get("notificationType") or ""))
    if event is None:
        return 200, {"accepted": True}
    data = _data(message, event)
    feedback_id = data.get("feedback_id")
    publish("email", event, data,
            source=data.get("source") or "ses",
            event_id=f"ses:{feedback_id}" if feedback_id else None)
    return 200, {"accepted": True}
