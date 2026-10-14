"""Relay slim inbound events: normalize and raw_mime rehydration."""

import hashlib

from email.message import EmailMessage

from src.dapier.connectors import ingress as ingress_module
from src.dapier.triggers import inbox
from src.dapier.triggers.intake import email_ingress


RAW_MIME_POINTER = {"bucket": "relay-bucket", "key": "raw/obj"}


class _Body:
    def __init__(self, data):
        self._data = data

    def read(self):
        return self._data


class _FakeS3:
    def __init__(self, objects):
        self.objects = objects
        self.puts = []

    def get_object(self, Bucket, Key):
        return {"Body": _Body(self.objects[(Bucket, Key)])}

    def put_object(self, **kwargs):
        self.puts.append(kwargs)


def _raw_email():
    message = EmailMessage()
    message["Message-ID"] = "<relay-1@inbound.relay.dtcdev.click>"
    message["From"] = "Billing <billing@example.com>"
    message["To"] = "invoice@inbound.relay.dtcdev.click"
    message["Subject"] = "July invoice"
    message.set_content("Invoice attached")
    message.add_alternative("<p>Invoice attached</p>", subtype="html")
    message.add_attachment(
        b"%PDF-1.4 fake", maintype="application", subtype="pdf", filename="invoice.pdf")
    return message.as_bytes()


def _relay_event():
    return {
        "contract": "inbound-email",
        "version": 1,
        "event_id": "relay-1",
        "event_type": "email.received",
        "occurred_at": "2026-10-14T08:00:00+00:00",
        "route": "invoice@inbound.relay.dtcdev.click",
        "message_id": "<relay-1@inbound.relay.dtcdev.click>",
        "sender": {"header": "Billing <billing@example.com>",
                   "addresses": ["billing@example.com"]},
        "recipients": {"addresses": ["invoice@inbound.relay.dtcdev.click"]},
        "subject": "July invoice",
        "inbound_message_id": 41,
        "raw_mime": dict(RAW_MIME_POINTER),
    }


def test_relay_slim_event_rehydrates_body_and_attachments(monkeypatch):
    fake = _FakeS3({("relay-bucket", "raw/obj"): _raw_email()})
    monkeypatch.setattr(ingress_module, "_s3", lambda: fake)
    monkeypatch.setenv("MAIL_BUCKET", "stage-bucket")

    event = ingress_module.normalize_event(_relay_event())

    assert event["id"] == event["correlation_id"] == "relay-1"
    assert event["source"] == "invoice"
    data = event["data"]
    assert data["route"] == "invoice"
    assert data["route_address"] == "invoice@inbound.relay.dtcdev.click"
    assert data["inbound_message_id"] == 41
    assert data["recipients"]["matched"] == ["invoice@inbound.relay.dtcdev.click"]
    assert data["body"]["text"]["value"].strip() == "Invoice attached"
    assert data["body"]["html"]["value"].strip() == "<p>Invoice attached</p>"
    assert data["html"] == data["body"]["html"]
    attachment = data["attachments"][0]
    assert attachment["filename"] == "invoice.pdf"
    assert attachment["content_type"] == "application/pdf"
    assert attachment["checksum"] == "sha256:" + hashlib.sha256(b"%PDF-1.4 fake").hexdigest()
    assert attachment["s3"]["bucket"] == "stage-bucket"
    assert attachment["s3"]["key"].startswith("relay-attachments/relay-1/")
    assert fake.puts[0]["Body"] == b"%PDF-1.4 fake"


def test_relay_slim_event_survives_rehydrate_failure(monkeypatch):
    def _down():
        raise RuntimeError("s3 unavailable")

    monkeypatch.setattr(ingress_module, "_s3", _down)

    event = ingress_module.normalize_event(_relay_event())

    data = event["data"]
    assert data["body"] is None
    assert data["attachments"] == []
    assert data["raw_mime"] == RAW_MIME_POINTER
    assert event["source"] == "invoice"


def test_datamailer_rich_event_normalizes_without_rehydrate(monkeypatch):
    def _unused():
        raise AssertionError("rich events must not touch s3")

    monkeypatch.setattr(ingress_module, "_s3", _unused)
    payload = {
        "contract": "inbound-email",
        "version": 1,
        "event_id": "sns-1",
        "event_type": "email.received",
        "occurred_at": "2026-07-12T10:00:00Z",
        "route": "newsletter",
        "message_id": "message-n",
        "sender": {"addresses": ["nope@example.com"], "header": "Nope"},
        "recipients": {"matched": ["newsletter@dtcdev.click"]},
        "subject": "Issue",
        "date": "2026-07-12T09:59:00Z",
        "body": {"html": {"value": "<p>hi</p>"}},
        "attachments": [],
        "raw_mime": {"bucket": "mail", "key": "raw/n"},
    }

    event = ingress_module.normalize_event(payload)

    data = event["data"]
    assert event["source"] == data["route"] == "newsletter"
    assert data["body"] == {"html": {"value": "<p>hi</p>"}}
    assert data["html"] == {"value": "<p>hi</p>"}
    assert data["attachments"] == []
    assert data["recipients"]["matched"] == ["newsletter@dtcdev.click"]


def test_trim_event_data_keeps_the_raw_mime_pointer():
    data = {"blob": "x" * 200, "raw_mime": {"bucket": "b", "key": "raw/k"},
            "message_id": "m"}

    trimmed = inbox._trim_event_data(data, limit=50)

    assert trimmed["truncated"] is True
    assert trimmed["raw_mime"] == {"bucket": "b", "key": "raw/k"}
    assert trimmed["message_id"] == "m"


def test_rehydrate_accepts_a_raw_mime_pointer(monkeypatch):
    def _replay(pointer, limit=email_ingress.BODY_LIMIT):
        assert pointer == {"bucket": "b", "key": "raw/k"}
        return {"text": "hi", "s3": pointer}, None

    monkeypatch.setattr(email_ingress, "replay_data", _replay)

    data, error = inbox.rehydrate({"raw_mime": {"bucket": "b", "key": "raw/k"}})

    assert error is None
    assert data["text"] == "hi"


def test_display_body_rebuilds_an_empty_body_from_raw_mime(monkeypatch):
    parsed = {"text": "full plain", "html": "<p>full</p>"}

    def _replay(pointer, limit=email_ingress.DISPLAY_BODY_LIMIT):
        return dict(parsed), None

    monkeypatch.setattr(email_ingress, "replay_data", _replay)

    content, rehydrated = inbox._display_body(
        {"raw_mime": {"bucket": "b", "key": "raw/k"}})

    assert rehydrated is True
    assert content == inbox.email_body(dict(parsed))
