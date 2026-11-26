"""Regression coverage for the original timestamp in clipped email replays."""
from email.message import EmailMessage
from unittest.mock import MagicMock

from src.dapier.api import runs
from src.dapier.connectors.ingress import normalize_event
from src.dapier.engine.actions import dataops as dataops_action
from src.dapier.triggers import inbox as inbox_store
from src.dapier.triggers.intake import email_ingress

ORIGINAL_DATE = "Tue, 06 Oct 2026 15:08:09 +0200"
ORIGINAL_MESSAGE_ID = "<original-newsletter@example.net>"
POINTER = {"bucket": "mail-bucket", "key": "raw/newsletter-1"}


def _raw_email():
    message = EmailMessage()
    message["From"] = "Sample Newsletter <publisher@example.net>"
    message["To"] = "newsletter@example.org"
    message["Subject"] = "Sample Newsletter #1"
    message["Message-ID"] = ORIGINAL_MESSAGE_ID
    message["Date"] = ORIGINAL_DATE
    message.set_content("x" * 80_000)
    return message.as_bytes()


def _s3_mock(raw):
    s3 = MagicMock()
    s3.get_object.return_value = {"Body": MagicMock(read=lambda: raw)}
    return s3


def _extraction_steps():
    return {"extract-sponsors": {"status": "completed", "output": {
        "ok": True,
        "data": {"newsletter": "Sample Newsletter", "sponsors": [{"name": "Sample Sponsor"}]},
        "model": "test-model", "usage": {}}}}


def test_data_from_message_keeps_raw_date():
    message = email_ingress.parse_message(_raw_email())
    data = email_ingress.data_from_message(message, dict(POINTER))
    assert data["date"] == ORIGINAL_DATE
    assert data["message_id"] == ORIGINAL_MESSAGE_ID


def test_replay_data_keeps_raw_date(monkeypatch):
    monkeypatch.setattr(email_ingress, "s3", _s3_mock(_raw_email()))
    data, error = email_ingress.replay_data(dict(POINTER))
    assert error is None
    assert data["date"] == ORIGINAL_DATE
    assert data["message_id"] == ORIGINAL_MESSAGE_ID


def test_runs_replay_clipped_keeps_original_timestamp(monkeypatch):
    monkeypatch.setattr(email_ingress, "s3", _s3_mock(_raw_email()))
    monkeypatch.setattr(
        inbox_store, "stored",
        lambda inbox_id, **kwargs: {
            "inbox_id": inbox_id,
            "data": {"truncated": True, "preview": "...", "s3": dict(POINTER)},
        })
    steps = [{"connector": "email", "event_type": "message.received",
              "input": {"truncated": True, "preview": "..."}}]
    event, error = runs.replay_event(
        "newsletter-sponsors:email:<orig>", steps)
    assert error is None
    # Flat rebuild keeps the raw Date for the reading views.
    assert event["data"]["date"] == ORIGINAL_DATE
    assert event["data"]["message_id"] == ORIGINAL_MESSAGE_ID
    # Contract carries the original timestamp, not the replay moment.
    assert event["date"] == ORIGINAL_DATE
    assert event["message_id"] == ORIGINAL_MESSAGE_ID
    # The worker normalizer reports the original receipt time.
    normalized = normalize_event(event)
    assert normalized["data"]["date"] == ORIGINAL_DATE
    assert normalized["data"]["message_id"] == ORIGINAL_MESSAGE_ID
    assert normalized["data"]["route"] == "newsletter"


def test_leads_received_at_matches_original_after_replay(monkeypatch):
    monkeypatch.setattr(email_ingress, "s3", _s3_mock(_raw_email()))
    monkeypatch.setattr(
        inbox_store, "stored",
        lambda inbox_id, **kwargs: {
            "inbox_id": inbox_id,
            "data": {"truncated": True, "preview": "...", "s3": dict(POINTER)},
        })
    steps = [{"connector": "email", "event_type": "message.received",
              "input": {"truncated": True, "preview": "..."}}]
    event, error = runs.replay_event(
        "newsletter-sponsors:email:<orig>", steps)
    assert error is None
    normalized = normalize_event(event)
    steps_map = _extraction_steps()
    action = {"type": "dataops", "connection_id": "dataops",
              "auth_secret_id": "dapier.dataops", "extraction_step": "extract-sponsors"}
    replay_body = dataops_action._leads_intake_body(action, normalized, steps=steps_map)
    # The original live delivery used the same Date header as its event time.
    original_event = {
        "connector": "email", "event": "message.received", "id": "email:<orig>",
        "occurred_at": ORIGINAL_DATE,
        "data": {"message_id": ORIGINAL_MESSAGE_ID, "route": "newsletter",
                 "subject": "Sample Newsletter #1", "date": ORIGINAL_DATE,
                 "sender": {"addresses": ["publisher@example.net"]}},
    }
    original_body = dataops_action._leads_intake_body(action, original_event, steps=steps_map)
    assert replay_body["messageId"] == original_body["messageId"] == ORIGINAL_MESSAGE_ID
    assert replay_body["newsletter"]["receivedAt"] == original_body["newsletter"]["receivedAt"]
    assert replay_body["newsletter"]["receivedAt"] == "2026-10-06T13:08:09Z"
    assert replay_body["sponsors"] == [{"name": "Sample Sponsor"}]
