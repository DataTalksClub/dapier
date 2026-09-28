"""The SES feedback intake: bounces and complaints to email trigger events.

SNS POSTs a JSON envelope to /hooks/ses-notifications (the configuration
set's feedback destination). A SubscriptionConfirmation is confirmed by
GETting its SubscribeURL; a Notification whose message is a Bounce publishes
email/bounce.received and a Complaint email/complaint.received — the events
stored watchers watch (email_triggers) and the email samples document
(connectors.email). Deliveries are accepted without publishing, the topic
allowlist (SES_NOTIFICATION_TOPICS) gates everything, and malformed input is
a 400 — never a 5xx SNS could retry-loop on.
"""
import json
from unittest.mock import MagicMock

import pytest

from src.dapier.triggers.intake import ses_notifications

TOPIC = "arn:aws:sns:us-east-1:817685572750:ses-feedback"

BOUNCE_MESSAGE = {
    "notificationType": "Bounce",
    "bounce": {
        "bounceType": "Permanent",
        "bounceSubType": "General",
        "bouncedRecipients": [{"emailAddress": "gone@example.test",
                               "action": "failed", "status": "5.1.1"}],
        "feedbackId": "0100000a-feedback-bounce",
        "timestamp": "2026-09-27T10:16:00Z",
    },
    "mail": {
        "messageId": "<m-4137@example.test>",
        "source": "billing@example.test",
        "destination": ["gone@example.test"],
        "timestamp": "2026-09-27T10:15:00Z",
    },
}


def _envelope(message, kind="Notification", topic=TOPIC):
    body = {"Type": kind, "MessageId": "sns-1", "TopicArn": topic, "Timestamp": "2026-09-27T10:16:00Z"}
    if message is not None:
        body["Message"] = json.dumps(message)
    if kind == "SubscriptionConfirmation":
        body["SubscribeURL"] = "https://sns.eu-west-1.amazonaws.com/?Action=ConfirmSubscription&TopicArn=" + topic
    return body


def _handle(envelope, publish):
    status, payload = ses_notifications.handle(
        json.dumps(envelope).encode(), publish=publish)
    return status, payload


# --- notifications publish the watcher events ------------------------------------


def test_bounce_publishes_bounce_received():
    published = []

    def publish(connector, event, data, source=None, event_id=None):
        published.append((connector, event, data, source, event_id))

    status, payload = _handle(_envelope(BOUNCE_MESSAGE), publish)

    assert status == 200 and payload == {"accepted": True}
    assert len(published) == 1
    connector, event, data, source, event_id = published[0]
    assert connector == "email" and event == "bounce.received"
    assert data["feedback_type"] == "bounce"
    assert data["bounce_type"] == "Permanent"
    assert data["bounce_subtype"] == "General"
    assert data["bounced_recipients"] == ["gone@example.test"]
    assert data["feedback_id"] == "0100000a-feedback-bounce"
    assert data["message_id"] == "<m-4137@example.test>"
    assert data["timestamp"] == "2026-09-27T10:16:00Z"
    assert data["source"] == "billing@example.test"
    assert data["destination"] == ["gone@example.test"]
    assert source == "billing@example.test"
    assert event_id == "ses:0100000a-feedback-bounce"


def test_complaint_publishes_complaint_received():
    message = {
        "notificationType": "Complaint",
        "complaint": {
            "complainedRecipients": [{"emailAddress": "annoyed@example.test"}],
            "feedbackId": "0100000a-feedback-complaint",
            "complaintFeedbackType": "abuse",
            "timestamp": "2026-09-27T10:17:00Z",
        },
        "mail": {"messageId": "<m-4137@example.test>",
                 "source": "billing@example.test",
                 "destination": ["annoyed@example.test"]},
    }
    published = []

    def publish(connector, event, data, source=None, event_id=None):
        published.append((connector, event, data))

    status, _payload = _handle(_envelope(message), publish)

    assert status == 200
    connector, event, data = published[0]
    assert connector == "email" and event == "complaint.received"
    assert data["feedback_type"] == "complaint"
    assert data["complained_recipients"] == ["annoyed@example.test"]
    assert data["feedback_id"] == "0100000a-feedback-complaint"
    assert data["message_id"] == "<m-4137@example.test>"
    assert data["source"] == "billing@example.test"
    assert data["destination"] == ["annoyed@example.test"]
    assert "bounce_type" not in data and "bounced_recipients" not in data


def test_delivery_is_accepted_without_publishing():
    published = MagicMock()
    status, payload = ses_notifications.handle(
        json.dumps(_envelope({"notificationType": "Delivery",
                              "mail": {"messageId": "<m-1>"}})).encode(),
        publish=published)

    assert status == 200 and payload == {"accepted": True}
    published.assert_not_called()


# --- subscription confirmation ----------------------------------------------------


def test_subscription_confirmation_gets_the_subscribe_url(monkeypatch):
    urlopen = MagicMock()
    monkeypatch.setattr(ses_notifications.urllib.request, "urlopen", urlopen)
    status, payload = ses_notifications.handle(
        json.dumps(_envelope(None, kind="SubscriptionConfirmation")).encode(),
        publish=MagicMock())

    assert status == 200 and payload == {"accepted": True, "confirmed": True}
    assert urlopen.call_count == 1
    assert "Action=ConfirmSubscription" in urlopen.call_args.args[0].full_url


def test_confirmation_survives_a_failed_subscribe_get(monkeypatch):
    import urllib.error

    monkeypatch.setattr(
        ses_notifications.urllib.request, "urlopen",
        MagicMock(side_effect=urllib.error.URLError("no dns")))
    status, payload = ses_notifications.handle(
        json.dumps(_envelope(None, kind="SubscriptionConfirmation")).encode(),
        publish=MagicMock())

    assert status == 200 and payload["confirmed"] is True


def test_unsubscribe_confirmation_is_never_fetched(monkeypatch):
    urlopen = MagicMock()
    monkeypatch.setattr(ses_notifications.urllib.request, "urlopen", urlopen)
    status, payload = ses_notifications.handle(
        json.dumps({"Type": "UnsubscribeConfirmation", "TopicArn": TOPIC,
                    "UnsubscribeURL": "https://sns.example.test/unsub"}).encode(),
        publish=MagicMock())

    assert status == 200
    urlopen.assert_not_called()


# --- the topic allowlist gates everything -----------------------------------------


@pytest.mark.parametrize("kind,message", [
    ("Notification", BOUNCE_MESSAGE),
    ("SubscriptionConfirmation", None),
])
def test_unknown_topic_is_403_and_nothing_runs(kind, message):
    published = MagicMock()
    with pytest.MonkeyPatch.context() as scope:
        scope.setenv("SES_NOTIFICATION_TOPICS",
                     "arn:aws:sns:us-east-1:817685572750:other-topic")
        status, payload = ses_notifications.handle(
            json.dumps(_envelope(message, kind=kind)).encode(), publish=published)

    assert status == 403 and "SES_NOTIFICATION_TOPICS" in payload["error"]
    published.assert_not_called()


def test_allowed_topic_is_accepted():
    published = MagicMock()
    with pytest.MonkeyPatch.context() as scope:
        scope.setenv("SES_NOTIFICATION_TOPICS", f" {TOPIC} ,arn:aws:sns:other")
        status, _payload = ses_notifications.handle(
            json.dumps(_envelope(BOUNCE_MESSAGE)).encode(), publish=published)

    assert status == 200
    published.assert_called_once()


def test_unset_allowlist_accepts_any_topic(monkeypatch):
    monkeypatch.delenv("SES_NOTIFICATION_TOPICS", raising=False)
    published = MagicMock()
    status, _payload = ses_notifications.handle(
        json.dumps(_envelope(BOUNCE_MESSAGE, topic="arn:aws:sns:unsanctioned")).encode(),
        publish=published)

    assert status == 200
    published.assert_called_once()


# --- malformed input settles, never 5xx -------------------------------------------


def test_malformed_bodies_are_400():
    published = MagicMock()
    for body in (b"not json", b"[]", b"", json.dumps({"Type": "Notification", "Message": "{oops"}).encode(),
                 json.dumps({"Type": "Notification", "Message": '"just a string"'}).encode(),
                 json.dumps({"Type": "Widget"}).encode()):
        status, payload = ses_notifications.handle(body, publish=published)
        assert status == 400, (body, status, payload)
    published.assert_not_called()


# --- the router serves the hook ---------------------------------------------------


def _post_ses(monkeypatch, envelope):
    from src.dapier.api import router as ingress

    sent = []
    monkeypatch.setenv("EVENT_QUEUE_URL", "https://sqs.example.test/events")
    monkeypatch.setattr(ingress.queue, "send_message", lambda **kwargs: sent.append(kwargs))
    response = ingress.handler({
        "requestContext": {"http": {"method": "POST", "path": "/hooks/ses-notifications"}},
        "headers": {"content-type": "text/plain; charset=UTF-8"},
        "body": json.dumps(envelope),
    }, None)
    return response, sent


def test_router_dispatches_the_ses_hook(monkeypatch):
    response, sent = _post_ses(monkeypatch, _envelope(BOUNCE_MESSAGE))

    assert response["statusCode"] == 200
    assert len(sent) == 1
    envelope = json.loads(sent[0]["MessageBody"])
    assert envelope["connector"] == "email"
    assert envelope["event"] == "bounce.received"
    assert envelope["data"]["bounce_type"] == "Permanent"
    assert envelope["data"]["bounced_recipients"] == ["gone@example.test"]


def test_router_rejects_an_unknown_topic_without_publishing(monkeypatch):
    with pytest.MonkeyPatch.context() as scope:
        scope.setenv("SES_NOTIFICATION_TOPICS", "arn:aws:sns:other")
        response, sent = _post_ses(monkeypatch, _envelope(BOUNCE_MESSAGE))

    assert response["statusCode"] == 403
    assert sent == []


def test_router_get_introspects(monkeypatch):
    from src.dapier.api import router as ingress

    response = ingress.handler({
        "requestContext": {"http": {"method": "GET", "path": "/hooks/ses-notifications"}},
        "queryStringParameters": None,
    }, None)
    assert response["statusCode"] == 200
    assert json.loads(response["body"])["hook"] == "ses-notifications"


# --- deployment wiring ------------------------------------------------------------


def test_template_wires_the_route_and_the_allowlist():
    template = open("template.yaml", encoding="utf-8").read()
    assert "Path: /hooks/ses-notifications, Method: GET" in template
    assert "Path: /hooks/ses-notifications, Method: POST" in template
    assert "SES_NOTIFICATION_TOPICS: !Ref SesNotificationTopics" in template
    assert "SesNotificationTopics:\n    Type: String\n    Default: ''" in template
    assert "SES_CONFIGURATION_SET: !Ref SesConfigurationSet" in template
    assert "SesConfigurationSet:\n    Type: String\n    Default: ''" in template


# --- sends carry the configuration set ----------------------------------------------

# Without a configuration set on the send, SES never reports bounce or
# complaint feedback — the hook above would only ever see what other senders
# produce. Simple and raw sends both name the deployment's set when
# SES_CONFIGURATION_SET is configured, and stay untagged when it isn't.

def _send(monkeypatch, ses, action=None, config_set="cs-feedback"):
    from src.dapier.engine.actions import email as email_action

    if config_set is None:
        monkeypatch.delenv("SES_CONFIGURATION_SET", raising=False)
    else:
        monkeypatch.setenv("SES_CONFIGURATION_SET", config_set)
    return email_action.run_email_send(
        {"type": "email_send", "to": "ops@example.com", "sender": "bot@x.test",
         "subject": "s", "text": "hi", **(action or {})},
        {"data": {}}, ses=ses)


class StubSes:
    def __init__(self):
        self.sent = []
        self.raw = []

    def send_email(self, **kwargs):
        self.sent.append(kwargs)
        return {"MessageId": "mid-1"}

    def send_raw_email(self, **kwargs):
        self.raw.append(kwargs)
        return {"MessageId": "raw-1"}


def test_a_simple_send_names_the_configuration_set(monkeypatch):
    ses = StubSes()

    _send(monkeypatch, ses)

    assert ses.sent[0]["ConfigurationSetName"] == "cs-feedback"


def test_a_raw_send_names_the_configuration_set(monkeypatch):
    ses = StubSes()

    _send(monkeypatch, ses, action={"in_reply_to": "<m-4137@example.test>"})

    assert ses.raw[0]["ConfigurationSetName"] == "cs-feedback"
    assert not ses.sent


def test_an_unset_configuration_set_sends_untagged(monkeypatch):
    ses = StubSes()

    _send(monkeypatch, ses, config_set=None)

    assert "ConfigurationSetName" not in ses.sent[0]


def test_a_blank_configuration_set_sends_untagged(monkeypatch):
    ses = StubSes()

    _send(monkeypatch, ses, config_set="   ")

    assert "ConfigurationSetName" not in ses.sent[0]
