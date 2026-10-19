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
    assert envelope["source"] == "todo"
    data = envelope["data"]
    assert data["from"] == "Acme Billing <billing@example.test>"
    assert data["to"] == "todo@dtcdev.click"
    assert data["route"] == "todo"
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


LEGACY_BUCKET = "datamailer-sandbox-817685572750-inbound-mail"


def _deliver_legacy(monkeypatch, raw, key="raw/legacy-1"):
    monkeypatch.setenv("LEGACY_INBOUND_BUCKET", LEGACY_BUCKET)
    return _deliver(monkeypatch, raw, bucket=LEGACY_BUCKET, key=key)


def test_legacy_datamailer_object_publishes_the_relay_contract(monkeypatch):
    envelope, _ = _deliver_legacy(monkeypatch, _message())

    assert envelope["contract"] == "inbound-email"
    assert envelope["version"] == 1
    assert envelope["event_id"] == "email:<m-4137@example.test>"
    assert envelope["route"] == "todo@dtcdev.click"
    assert envelope["message_id"] == "<m-4137@example.test>"
    assert envelope["sender"] == {"header": "Acme Billing <billing@example.test>",
                                  "addresses": ["billing@example.test"]}
    assert envelope["recipients"] == {"addresses": ["todo@dtcdev.click"]}
    assert envelope["subject"] == "Invoice #4137 - September"
    assert envelope["raw_mime"] == {"bucket": LEGACY_BUCKET, "key": "raw/legacy-1"}


def test_the_contract_normalizes_to_a_routeable_event(monkeypatch):
    from src.dapier.connectors import ingress as ingress_module

    envelope, _ = _deliver_legacy(monkeypatch, _message())
    event = ingress_module.normalize_event(envelope)

    assert event["connector"] == "email"
    assert event["source"] == "todo"
    assert event["data"]["route"] == "todo"
    assert event["data"]["raw_mime"] == {"bucket": LEGACY_BUCKET, "key": "raw/legacy-1"}


def test_a_legacy_object_without_message_id_uses_the_key_as_id(monkeypatch):
    message = EmailMessage()
    message["From"] = "a@example.test"
    message["To"] = "agents@dtcdev.click"
    message["Subject"] = "no id"
    message.set_content("hello")

    envelope, _ = _deliver_legacy(monkeypatch, message.as_bytes(), key="raw/no-id-1")

    assert envelope["event_id"] == "email:raw/no-id-1"


def test_staged_attachment_objects_do_not_reenter_the_intake(monkeypatch):
    s3 = MagicMock()
    queue = MagicMock()
    monkeypatch.setattr(email_ingress, "s3", s3)
    monkeypatch.setattr(email_ingress, "queue", queue)
    email_ingress.handler({"Records": [
        {"s3": {"bucket": {"name": "dapier-mail-inbound"},
                "object": {"key": email_ingress.STAGED_ATTACHMENT_PREFIX
                           + "replay-4b689feb/attachments/005-Rechnung_1.pdf"}}}]},
        None)

    s3.get_object.assert_not_called()
    queue.send_message.assert_not_called()


def test_stager_keys_live_under_the_intake_skip_prefix(monkeypatch):
    from email.message import EmailMessage

    from src.dapier.connectors import ingress as ingress_module

    s3 = MagicMock()
    monkeypatch.setattr(ingress_module, "_s3", lambda: s3)
    monkeypatch.setenv("MAIL_BUCKET", "stage-bucket")
    message = EmailMessage()
    message.add_attachment(b"%PDF-1.4", maintype="application",
                           subtype="pdf", filename="Rechnung 1.pdf")

    staged = ingress_module._stage_attachments(message, "replay-4b689feb")

    assert staged[0]["s3"]["key"].startswith(
        email_ingress.STAGED_ATTACHMENT_PREFIX
        + "replay-4b689feb/attachments/")
    assert staged[0]["s3"]["key"].endswith("-Rechnung_1.pdf")



def test_inbound_email_dev_and_tofu_match_bot_mailbox_triggers(monkeypatch):
    from src.dapier.engine import matching

    dev_msg = EmailMessage()
    dev_msg["From"] = "alexey@datatalks.club"
    dev_msg["To"] = "dev@dtcdev.click"
    dev_msg["Subject"] = "dev mailbox test"
    dev_msg["Message-ID"] = "<dev-msg-1@example.com>"
    dev_msg.set_content("ping dev")

    envelope, _ = _deliver(monkeypatch, dev_msg.as_bytes(), key="raw/dev-1")
    assert envelope["source"] == "dev"
    assert envelope["data"]["route"] == "dev"
    assert envelope["data"]["to"] == "dev@dtcdev.click"

    dev_workflow = {
        "id": "dev-mailbox",
        "enabled": True,
        "triggers": [{
            "connector": "email",
            "event": "message.received",
            "filters": {"route": {"equals": "dev"}},
        }],
    }
    assert matching.matches(dev_workflow, envelope) is True

    tofu_msg = EmailMessage()
    tofu_msg["From"] = "alexey@datatalks.club"
    tofu_msg["To"] = "tofu@dtcdev.click"
    tofu_msg["Subject"] = "tofu mailbox test"
    tofu_msg["Message-ID"] = "<tofu-msg-1@example.com>"
    tofu_msg.set_content("ping tofu")

    tofu_env, _ = _deliver(monkeypatch, tofu_msg.as_bytes(), key="raw/tofu-1")
    assert tofu_env["source"] == "tofu"
    assert tofu_env["data"]["route"] == "tofu"

    tofu_workflow = {
        "id": "tofu-mailbox",
        "enabled": True,
        "triggers": [{
            "connector": "email",
            "event": "message.received",
            "filters": {"route": {"equals": "tofu"}},
        }],
    }
    assert matching.matches(tofu_workflow, tofu_env) is True


if __name__ == "__main__":
    import pytest

    pytest.main([__file__])
