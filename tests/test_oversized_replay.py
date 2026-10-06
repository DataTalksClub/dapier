"""Oversized event replay: a clipped trigger rehydrates from the raw email
copy its s3 pointer names instead of refusing.

Events bigger than the trigger-input cap (engine.worker.TRIGGER_INPUT_LIMIT)
store a truncated preview; the inbox row keeps the s3 pointer and
message_id alongside it, and the runs/inbox replay paths rebuild the full
data from the raw MIME in the mail bucket.
"""
import json
from email.message import EmailMessage
from unittest.mock import MagicMock

import boto3
import pytest

from src.dapier.api import runs as runs_api
from src.dapier.triggers import inbox
from src.dapier.triggers.intake import email_ingress


def _raw(size=80_000):
    message = EmailMessage()
    message["From"] = "Acme Billing <billing@example.test>"
    message["To"] = "agents@dtcdev.click"
    message["Subject"] = "Fwd: Meeting assets are ready"
    message["Message-ID"] = "<big-1@example.test>"
    message.set_content("x" * size)
    return message.as_bytes()


def _s3_mock(raw):
    s3 = MagicMock()
    s3.get_object.return_value = {"Body": MagicMock(read=lambda: raw)}
    return s3


POINTER = {"bucket": "dapier-mail-inbound", "key": "raw/big-1"}


def _oversized_event():
    return {
        "schema_version": "1.0",
        "id": "email:<big-1@example.test>",
        "connector": "email",
        "event": "message.received",
        "source": "agents@dtcdev.click",
        "occurred_at": "2026-10-06T09:00:00+00:00",
        "data": {"message_id": "<big-1@example.test>",
                 "text": "x" * 80_000, "s3": POINTER},
    }


class FakeTable:
    def __init__(self):
        self.items = {}

    def put_item(self, Item, **kwargs):
        self.items[Item["inbox_id"]] = dict(Item)

    def get_item(self, Key, **kwargs):
        item = self.items.get(Key["inbox_id"])
        return {"Item": dict(item)} if item else {}


class FakeQueue:
    def __init__(self):
        self.messages = []

    def send_message(self, QueueUrl, MessageBody):
        self.messages.append((QueueUrl, MessageBody))


@pytest.fixture
def table(monkeypatch):
    fake = FakeTable()
    monkeypatch.setenv(inbox.TABLE_ENV, "trigger-inbox")

    class Dynamo:
        def Table(self, _name):
            return fake

    monkeypatch.setattr(boto3, "resource", lambda service: Dynamo())
    return fake


def test_record_keeps_the_replay_pointers_when_clipping(table):
    inbox.record(_oversized_event(), table_ref=table)

    stored = table.items["email:<big-1@example.test>"]["data"]
    assert stored["truncated"] is True
    assert len(stored["preview"]) == inbox.DATA_LIMIT
    assert stored["s3"] == POINTER
    assert stored["message_id"] == "<big-1@example.test>"


def test_inbox_replay_rehydrates_a_clipped_email(table, monkeypatch):
    inbox.record(_oversized_event(), table_ref=table)
    monkeypatch.setattr(email_ingress, "s3", _s3_mock(_raw()))
    queue = FakeQueue()
    monkeypatch.setenv("EVENT_QUEUE_URL", "https://queue")

    status, payload = inbox.api_replay(
        "email:<big-1@example.test>", queue=queue, table_ref=table)

    assert status == 202
    assert payload["replayed_from"] == "email:<big-1@example.test>"
    sent = json.loads(queue.messages[0][1])
    assert sent["correlation_id"] == "email:<big-1@example.test>"
    # The rehydrated data matches what the intake originally delivered:
    # same per-part cap, pointer intact.
    assert len(sent["data"]["text"]) == email_ingress.BODY_LIMIT
    assert sent["data"]["message_id"] == "<big-1@example.test>"
    assert sent["data"]["s3"] == POINTER


def test_inbox_replay_refuses_when_the_raw_copy_is_gone(table, monkeypatch):
    table.items["email:<big-1@example.test>"] = {
        "inbox_id": "email:<big-1@example.test>",
        "connector": "email", "event": "message.received",
        "data": {"truncated": True, "preview": "...", "s3": POINTER},
    }
    s3 = MagicMock()
    s3.get_object.side_effect = RuntimeError("no such key")
    monkeypatch.setattr(email_ingress, "s3", s3)

    status, payload = inbox.api_replay(
        "email:<big-1@example.test>", queue=FakeQueue(), table_ref=table)

    assert status == 409
    assert "cannot be replayed" in payload["error"]


def test_inbox_replay_still_refuses_without_a_pointer(table):
    table.items["evt-big"] = {
        "inbox_id": "evt-big", "connector": "email",
        "event": "message.received",
        "data": {"truncated": True, "preview": "..."},
    }
    status, _payload = inbox.api_replay(
        "evt-big", queue=FakeQueue(), table_ref=table)
    assert status == 409


def test_runs_replay_rehydrates_from_the_inbox_row(monkeypatch):
    steps = [{"connector": "email", "event_type": "message.received",
              "input": {"truncated": True, "preview": "..." * 100}}]
    monkeypatch.setattr(
        inbox, "stored",
        lambda inbox_id, **kwargs: {
            "inbox_id": inbox_id,
            "data": {"truncated": True, "preview": "...", "s3": POINTER},
        })
    monkeypatch.setattr(email_ingress, "s3", _s3_mock(_raw()))

    event, error = runs_api.replay_event(
        "email-trigger-agents:email:<big-1@example.test>", steps)

    assert error is None
    assert event["correlation_id"] == "email:<big-1@example.test>"
    assert event["connector"] == "email"
    assert len(event["data"]["text"]) == email_ingress.BODY_LIMIT
    assert event["data"]["s3"] == POINTER


def test_runs_replay_refuses_when_the_inbox_row_has_no_pointer(monkeypatch):
    steps = [{"connector": "email", "event_type": "message.received",
              "input": {"truncated": True, "preview": "..." * 100}}]
    monkeypatch.setattr(inbox, "stored", lambda inbox_id, **kwargs: None)

    event, error = runs_api.replay_event(
        "email-trigger-agents:email:<big-1@example.test>", steps)

    assert event is None
    assert "cannot be replayed" in error


def test_replay_data_rejects_an_unusable_pointer():
    data, error = email_ingress.replay_data({"bucket": "b"})
    assert data is None
    assert "pointer" in error
