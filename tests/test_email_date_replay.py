"""Clipped newsletter replay preserves the original email timestamp.

Live 2026-11-25 Postgres Weekly/Tiger Data case: the original run succeeded
via normal send, then `dapier runs replay --from-step forward-leads`
rehydrated the clipped trigger from the raw MIME but rebuilt the input date
as the replay moment (2026-11-26T03:39...), so DataOps rejected the same
message identity with 409. The raw Date header must survive the
flat rebuild (data_from_message), the contract, the worker normalizer, and
the leads receivedAt — with the stored extraction untouched.
"""
from email.message import EmailMessage
from unittest.mock import MagicMock

from src.dapier.api import runs
from src.dapier.connectors.ingress import normalize_event
from src.dapier.engine.actions import dataops as dataops_action
from src.dapier.triggers import inbox as inbox_store
from src.dapier.triggers.intake import email_ingress

ORIGINAL_DATE = "Wed, 25 Nov 2026 17:01:17 +0000"
ORIGINAL_MESSAGE_ID = "<6fCNNo9HRByu-OjaqeDtCQ@geopod-ismtpd-12>"
POINTER = {"bucket": "mail-bucket", "key": "raw/newsletter-1"}


def _raw_email():
    message = EmailMessage()
    message["From"] = "Postgres Weekly <postgres@cooperpress.com>"
    message["To"] = "newsletter@dtcdev.click"
    message["Subject"] = "Postgres Weekly #675"
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
        "data": {"newsletter": "Postgres Weekly", "sponsors": [{"name": "Tiger Data"}]},
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
                 "subject": "Postgres Weekly #675", "date": ORIGINAL_DATE,
                 "sender": {"addresses": ["postgres@cooperpress.com"]}},
    }
    original_body = dataops_action._leads_intake_body(action, original_event, steps=steps_map)
    assert replay_body["messageId"] == original_body["messageId"] == ORIGINAL_MESSAGE_ID
    assert replay_body["newsletter"]["receivedAt"] == original_body["newsletter"]["receivedAt"]
    assert replay_body["newsletter"]["receivedAt"] == "2026-11-25T17:01:17Z"
    assert replay_body["sponsors"] == [{"name": "Tiger Data"}]
