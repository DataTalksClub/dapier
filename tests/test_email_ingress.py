"""The raw-MIME email intake: headers, decoded body, and the s3 pointer.

The SES catch-all drops the raw message in S3 and this handler publishes
the event: the headers workflows filter on, plus the decoded text/html
bodies (capped, so an ordinary message stays replayable) and the s3
pointer back to the untouched raw MIME.
"""
import json
from email.message import EmailMessage
from unittest.mock import MagicMock

from src.dapier.triggers.intake import email_ingress


def _message(text="Invoice #4137 for September is attached.",
             html="<p>Invoice #4137 for September is attached.</p>"):
    message = EmailMessage()
    message["From"] = "Acme Billing <billing@example.test>"
    message["To"] = "todo@dtcdev.click"
    message["Subject"] = "Invoice #4137 - September"
    message["Message-ID"] = "<m-4137@example.test>"
    message.set_content(text)
    message.add_alternative(html, subtype="html")
    return message.as_bytes()


def _deliver(monkeypatch, raw, bucket="dapier-mail-inbound", key="raw/m-4137"):
    s3 = MagicMock()
    s3.get_object.return_value = {"Body": MagicMock(read=lambda: raw)}
    queue = MagicMock()
    monkeypatch.setattr(email_ingress, "s3", s3)
    monkeypatch.setattr(email_ingress, "queue", queue)
    monkeypatch.setenv("EVENT_QUEUE_URL", "https://queue.test/events")
    email_ingress.handler({"Records": [
        {"s3": {"bucket": {"name": bucket}, "object": {"key": key}}}]}, None)
    call = queue.send_message.call_args
    envelope = json.loads(call.kwargs["MessageBody"])
    return envelope, s3


def test_publishes_headers_decoded_body_and_the_s3_pointer(monkeypatch):
    envelope, s3 = _deliver(monkeypatch, _message())

    s3.get_object.assert_called_once_with(Bucket="dapier-mail-inbound",
                                          Key="raw/m-4137")
    assert envelope["id"] == "email:<m-4137@example.test>"
    assert envelope["connector"] == "email"
    assert envelope["event"] == "message.received"
    data = envelope["data"]
    assert data["from"] == "Acme Billing <billing@example.test>"
    assert data["to"] == "todo@dtcdev.click"
    assert data["subject"] == "Invoice #4137 - September"
    assert data["message_id"] == "<m-4137@example.test>"
    assert data["text"] == "Invoice #4137 for September is attached.\n"
    assert data["html"] == "<p>Invoice #4137 for September is attached.</p>\n"
    assert data["s3"] == {"bucket": "dapier-mail-inbound", "key": "raw/m-4137"}


def test_a_plain_only_message_publishes_text_and_null_html(monkeypatch):
    message = EmailMessage()
    message["From"] = "a@example.test"
    message["To"] = "todo@dtcdev.click"
    message["Subject"] = "plain"
    message["Message-ID"] = "<plain-1@example.test>"
    message.set_content("just text")

    envelope, _s3 = _deliver(monkeypatch, message.as_bytes())

    assert envelope["data"]["text"] == "just text\n"
    assert envelope["data"]["html"] is None


def test_body_parts_are_capped_at_the_publish_limit(monkeypatch):
    envelope, _s3 = _deliver(
        monkeypatch, _message(text="x" * 70_000, html="<b>" + "y" * 70_000 + "</b>"))

    assert len(envelope["data"]["text"]) == email_ingress.BODY_LIMIT
    assert len(envelope["data"]["html"]) == email_ingress.BODY_LIMIT


if __name__ == "__main__":
    import pytest

    pytest.main([__file__])
