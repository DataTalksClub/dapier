import json
import unittest

from src.worker import normalize_payload


class NormalizeTests(unittest.TestCase):
    def test_normalizes_raw_sns_inbound_email(self):
        email = {
            "contract": "inbound-email",
            "version": 1,
            "event_id": "event-1",
            "event_type": "email.received",
            "occurred_at": "2026-07-12T10:00:00Z",
            "route": "todo",
            "message_id": "message-1",
            "sender": {"addresses": ["sender@example.com"]},
            "recipients": {"matched": ["todo@example.com"]},
            "subject": "Do this",
            "date": "2026-07-12T09:59:00Z",
            "body": {"html": {"value": "<p>Do this</p>"}},
            "attachments": [],
            "raw_mime": {"bucket": "mail", "key": "raw/1"},
        }
        result = normalize_payload({"Type": "Notification", "Message": json.dumps(email)})
        self.assertEqual(result["schema_version"], "1.0")
        self.assertEqual(result["correlation_id"], "event-1")
        self.assertEqual(result["connector"], "email")
        self.assertEqual(result["data"]["route"], "todo")

    def test_normalizes_renderer_completion(self):
        result = normalize_payload({
            "schema": "html-renderer.completed.v1",
            "job_id": "job-1",
            "timestamp": "2026-07-12T10:00:00Z",
            "output": {"bucket": "renders", "key": "one.pdf"},
            "content_type": "application/pdf",
            "size_bytes": 42,
            "checksum": "abc",
            "context": {"source_event": {"data": {"message_id": "m1", "route": "invoice-pdf"}}},
        })
        self.assertEqual(result["event"], "job.completed")
        self.assertEqual(result["data"]["size_bytes"], 42)

    def test_rejects_unsupported_inbound_email_contract_version(self):
        with self.assertRaisesRegex(ValueError, "unsupported inbound-email contract version"):
            normalize_payload({"contract": "inbound-email", "version": 2})


if __name__ == "__main__":
    unittest.main()
