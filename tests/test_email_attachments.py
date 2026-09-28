"""email_send's reply_to/cc/bcc fields and attachments.

Plain sends keep the exact simple send_email call earlier workflows made.
The new recipient fields slot into that call; any send with attachments
switches to raw MIME (EmailMessage), which always carries an explicit
Message-ID — the inbound side dedupes on it — and one part per attachment
with its guessed content type.
"""
import re
import unittest
from email import policy
from email.parser import BytesParser

from src.dapier.engine.actions import email as email_action


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


def download_returns(status=200, raw=b"%PDF-bytes", calls=None):
    def transport(method, url, *, headers=None, body=None, timeout=15):
        if calls is not None:
            calls.append({"method": method, "url": url, "headers": headers,
                          "body": body, "timeout": timeout})
        return status, raw

    return transport


def parse_raw(ses):
    return BytesParser(policy=policy.default).parsebytes(ses.raw[0]["RawMessage"]["Data"])


class PlainSendParityTests(unittest.TestCase):
    """No new fields — the exact simple send_email call as before."""

    def setUp(self):
        self.ses = StubSes()

    def test_plain_send_keeps_the_simple_call_and_output(self):
        output = email_action.run_email_send(
            {"type": "email_send", "to": "ops@example.com", "sender": "bot@x.test",
             "subject": "s", "text": "hi"},
            {"data": {}}, ses=self.ses)
        call = self.ses.sent[0]
        self.assertEqual(call["Source"], "bot@x.test")
        self.assertEqual(call["Destination"], {"ToAddresses": ["ops@example.com"]})
        self.assertNotIn("ReplyToAddresses", call)
        self.assertEqual(call["Message"]["Body"]["Text"]["Data"], "hi")
        self.assertEqual(self.ses.raw, [])
        self.assertEqual(output, {"message_id": "mid-1", "to": ["ops@example.com"], "subject": "s"})


class RecipientFieldTests(unittest.TestCase):
    def setUp(self):
        self.ses = StubSes()

    def run_action(self, action):
        return email_action.run_email_send(
            {"type": "email_send", "to": "ops@example.com", "sender": "bot@x.test",
             "text": "hi", **action}, {"data": {"copy": "cc@example.com"}}, ses=self.ses)

    def test_reply_to_rides_the_simple_call(self):
        self.run_action({"reply_to": "replies@x.test"})
        call = self.ses.sent[0]
        self.assertEqual(call["ReplyToAddresses"], ["replies@x.test"])
        self.assertEqual(call["Destination"], {"ToAddresses": ["ops@example.com"]})

    def test_cc_and_bcc_land_in_the_destination(self):
        self.run_action({"cc": "copy@x.test, {copy}", "bcc": "hidden@x.test"})
        call = self.ses.sent[0]
        self.assertEqual(call["Destination"], {"ToAddresses": ["ops@example.com"],
                                               "CcAddresses": ["copy@x.test", "cc@example.com"],
                                               "BccAddresses": ["hidden@x.test"]})

    def test_recipient_lists_accept_lists_and_templates(self):
        output = self.run_action({"reply_to": ["a@x.test", "b@x.test"],
                                  "cc": ["{copy}"], "bcc": "h1@x.test, h2@x.test"})
        call = self.ses.sent[0]
        self.assertEqual(call["ReplyToAddresses"], ["a@x.test", "b@x.test"])
        self.assertEqual(call["Destination"]["CcAddresses"], ["cc@example.com"])
        self.assertEqual(call["Destination"]["BccAddresses"], ["h1@x.test", "h2@x.test"])
        self.assertEqual(output["cc"], ["cc@example.com"])
        self.assertEqual(output["bcc"], ["h1@x.test", "h2@x.test"])


class AttachmentSendTests(unittest.TestCase):
    def setUp(self):
        self.ses = StubSes()

    def run_action(self, action, *, transport=None, event=None):
        return email_action.run_email_send(
            {"type": "email_send", "to": "ops@example.com", "sender": "bot@x.test",
             "subject": "report", "text": "attached", **action},
            event or {"data": {}}, ses=self.ses, transport=transport)

    def test_inline_content_rides_raw_mime_with_message_id(self):
        self.run_action({"attachments": [{"filename": "report.pdf", "content": "%PDF-inline"}]})
        message = parse_raw(self.ses)
        self.assertEqual(message["From"], "bot@x.test")
        self.assertEqual(message["To"], "ops@example.com")
        self.assertEqual(message["Subject"], "report")
        self.assertTrue(re.fullmatch(r"<[^>]+@[^>]+>", message["Message-ID"]))
        attachments = list(message.iter_attachments())
        self.assertEqual(len(attachments), 1)
        self.assertEqual(attachments[0].get_filename(), "report.pdf")
        self.assertEqual(attachments[0].get_content_type(), "application/pdf")
        self.assertEqual(attachments[0].get_content(), b"%PDF-inline")
        self.assertEqual(self.ses.raw[0]["RawMessage"]["Data"], message.as_bytes())

    def test_text_and_html_parts_survive_the_raw_path(self):
        self.run_action({"text": "plain copy", "html": "<b>rich</b>",
                         "attachments": [{"filename": "note.txt", "content": "note"}]})
        message = parse_raw(self.ses)
        bodies = [part for part in message.walk() if part.get_content_type() == "text/plain"
                  and not part.get_filename()]
        html = [part for part in message.walk() if part.get_content_type() == "text/html"]
        self.assertEqual(len(bodies), 1)
        self.assertEqual(bodies[0].get_content().strip(), "plain copy")
        self.assertEqual(html[0].get_content().strip(), "<b>rich</b>")

    def test_source_url_download_becomes_the_attachment(self):
        calls = []
        self.run_action({"attachments": [{"filename": "invoice.pdf",
                                          "source_url": "https://files.test/i.pdf"}]},
                        transport=download_returns(calls=calls))
        self.assertEqual(calls[0]["method"], "GET")
        self.assertEqual(calls[0]["url"], "https://files.test/i.pdf")
        attachment = parse_raw(self.ses).iter_attachments().__next__()
        self.assertEqual(attachment.get_content(), b"%PDF-bytes")

    def test_raw_path_carries_reply_to_cc_and_bcc_headers(self):
        self.run_action({"reply_to": "replies@x.test", "cc": "copy@x.test",
                         "bcc": "hidden@x.test",
                         "attachments": [{"filename": "note.txt", "content": "note"}]})
        message = parse_raw(self.ses)
        self.assertEqual(message["Reply-To"], "replies@x.test")
        self.assertEqual(message["Cc"], "copy@x.test")
        self.assertEqual(message["Bcc"], "hidden@x.test")

    def test_filenames_are_templated(self):
        self.run_action({"attachments": [{"filename": "{name}.pdf", "content": "x"}]},
                        event={"data": {"name": "invoice"}})
        message = parse_raw(self.ses)
        self.assertEqual(list(message.iter_attachments())[0].get_filename(), "invoice.pdf")

    def test_download_error_statuses_are_clear_runtime_errors(self):
        with self.assertRaises(RuntimeError, msg="attachment download returned HTTP 500"):
            self.run_action({"attachments": [{"filename": "a.pdf",
                                              "source_url": "https://files.test/a.pdf"}]},
                            transport=download_returns(status=500))

    def test_unreachable_downloads_are_clear_runtime_errors(self):
        def broken(method, url, *, headers=None, body=None, timeout=15):
            raise ConnectionError("no route")

        with self.assertRaises(RuntimeError, msg="attachment download unreachable: ConnectionError"):
            self.run_action({"attachments": [{"filename": "a.pdf",
                                              "source_url": "https://files.test/a.pdf"}]},
                            transport=broken)

    def test_attachment_validation_is_explicit(self):
        with self.assertRaises(ValueError, msg="needs a filename"):
            self.run_action({"attachments": [{"content": "x"}]})
        with self.assertRaises(ValueError, msg="takes content or source_url, not both"):
            self.run_action({"attachments": [{"filename": "a.txt", "content": "x",
                                              "source_url": "https://files.test/a"}]})
        with self.assertRaises(ValueError, msg="needs content or source_url"):
            self.run_action({"attachments": [{"filename": "a.txt"}]})
        with self.assertRaises(ValueError, msg="must be a list"):
            self.run_action({"attachments": {"filename": "a.txt", "content": "x"}})
        self.assertEqual(self.ses.raw, [])

    def test_every_message_id_is_unique(self):
        for _ in range(2):
            self.run_action({"attachments": [{"filename": "a.txt", "content": "x"}]})
        first = parse_raw(self.ses)
        second = BytesParser(policy=policy.default).parsebytes(
            self.ses.raw[1]["RawMessage"]["Data"])
        self.assertNotEqual(first["Message-ID"], second["Message-ID"])

    def test_raw_send_reports_the_raw_message_id(self):
        output = self.run_action({"attachments": [{"filename": "a.txt", "content": "x"}]})
        self.assertEqual(output, {"message_id": "raw-1", "to": ["ops@example.com"],
                                  "subject": "report"})


if __name__ == "__main__":
    import pytest

    pytest.main([__file__])
