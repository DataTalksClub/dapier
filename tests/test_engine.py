import json
import unittest
from copy import deepcopy
from unittest.mock import patch

from src.engine import matches, run_dropbox_upload, run_slack


class FakeTransport:
    def __init__(self, status=200, body=b'{"path_display": "/Invoices/x"}'):
        self.status = status
        self.body = body
        self.calls = []

    def __call__(self, method, url, *, headers, body, timeout=15):
        self.calls.append({"method": method, "url": url, "headers": headers, "body": body})
        return self.status, self.body


class MatchTests(unittest.TestCase):
    def test_matches_nested_string_rules(self):
        workflow = {
            "enabled": True,
            "trigger": {
                "connector": "dropbox",
                "event": "file.created",
                "filters": {"path": {"prefix": "/incoming/", "suffix": ".pdf"}},
            },
        }
        event = {"connector": "dropbox", "event": "file.created", "data": {"path": "/incoming/a.pdf"}}
        self.assertTrue(matches(workflow, event))

    def test_rejects_wrong_event(self):
        workflow = {"enabled": True, "trigger": {"connector": "youtube", "event": "video.published"}}
        event = {"connector": "dropbox", "event": "video.published", "data": {}}
        self.assertFalse(matches(workflow, event))


class SlackTests(unittest.TestCase):
    @patch("src.engine._json_request")
    @patch("src.engine.get_credential")
    def test_reads_bot_token_from_dynamodb_credential(self, get_credential, json_request):
        get_credential.return_value = {"token": "xoxb-private"}
        json_request.return_value = {"ok": True}

        run_slack(
            {"credential_id": "slack", "channel": "C123", "text": "{title}: {url}"},
            {"data": {"title": "Published", "url": "https://example.test/video"}},
        )

        get_credential.assert_called_once_with("slack")
        self.assertEqual(json_request.call_args.args[1]["channel"], "C123")
        self.assertEqual(json_request.call_args.args[1]["text"], "Published: https://example.test/video")
        self.assertEqual(json_request.call_args.kwargs["headers"], {"authorization": "Bearer xoxb-private"})


class DropboxUploadTests(unittest.TestCase):
    def setUp(self):
        self.connection = {"connection_id": "dropbox", "provider": "dropbox", "status": "connected"}
        self.attachment_event = {
            "data": {
                "attachments": [{
                    "filename": "invoice.pdf",
                    "s3": {"bucket": "mail", "key": "msg/invoice.pdf"},
                }],
            },
        }

    def run_action(self, transport, event=None, action=None):
        action = action or {"type": "dropbox_upload", "connection_id": "dropbox", "folder": "/Invoices"}
        with patch("src.engine._dropbox_connection", return_value=dict(self.connection)), \
             patch("src.tokens.get_access_token", return_value=("token-123", {})), \
             patch("src.engine._s3_body", return_value=b"pdf-bytes"):
            run_dropbox_upload(action, event or deepcopy(self.attachment_event), transport=transport)

    def test_uploads_attachment_to_folder(self):
        transport = FakeTransport()
        self.run_action(transport)

        call = transport.calls[0]
        self.assertEqual(call["method"], "POST")
        self.assertEqual(call["url"], "https://content.dropboxapi.com/2/files/upload")
        self.assertEqual(call["headers"]["authorization"], "Bearer token-123")
        arg = json.loads(call["headers"]["dropbox-api-arg"])
        self.assertEqual(arg["path"], "/Invoices/invoice.pdf")
        self.assertTrue(arg["autorename"])
        self.assertEqual(call["body"], b"pdf-bytes")

    def test_uploads_every_attachment(self):
        transport = FakeTransport()
        event = {"data": {"attachments": [
            {"filename": "a.pdf", "s3": {"bucket": "mail", "key": "a.pdf"}},
            {"filename": "b.pdf", "s3": {"bucket": "mail", "key": "b.pdf"}},
        ]}}
        self.run_action(transport, event=event)

        paths = [json.loads(c["headers"]["dropbox-api-arg"])["path"] for c in transport.calls]
        self.assertEqual(paths, ["/Invoices/a.pdf", "/Invoices/b.pdf"])

    def test_uploads_rendered_output(self):
        transport = FakeTransport()
        action = {
            "type": "dropbox_upload", "connection_id": "dropbox",
            "source": "output", "folder": "/Invoices", "filename": "invoice-email.pdf",
        }
        event = {"data": {"output": {"bucket": "artifacts", "key": "rendered/1.pdf"}}}
        self.run_action(transport, event=event, action=action)

        arg = json.loads(transport.calls[0]["headers"]["dropbox-api-arg"])
        self.assertEqual(arg["path"], "/Invoices/invoice-email.pdf")

    def test_strips_path_traversal_from_filename(self):
        transport = FakeTransport()
        event = {"data": {"attachments": [
            {"filename": "../../etc/cron.d/invoice.pdf", "s3": {"bucket": "mail", "key": "x"}},
        ]}}
        self.run_action(transport, event=event)

        arg = json.loads(transport.calls[0]["headers"]["dropbox-api-arg"])
        self.assertEqual(arg["path"], "/Invoices/invoice.pdf")

    def test_fails_without_attachments(self):
        with self.assertRaises(ValueError):
            self.run_action(FakeTransport(), event={"data": {"attachments": []}})

    def test_fails_when_connection_not_connected(self):
        with patch("boto3.resource") as resource, \
             patch("src.connections.get_connection",
                   return_value={"connection_id": "dropbox", "provider": "dropbox", "status": "ready"}), \
             patch.dict("os.environ", {"CONNECTIONS_TABLE": "connections"}), \
             self.assertRaises(ValueError) as ctx:
            run_dropbox_upload(
                {"type": "dropbox_upload", "connection_id": "dropbox"},
                {"data": {}}, transport=FakeTransport(),
            )
        self.assertIn("not connected", str(ctx.exception))
        resource.assert_called_once_with("dynamodb")

    def test_raises_on_dropbox_conflict(self):
        transport = FakeTransport(status=409, body=b'{"error": {".tag": "path", "path": {".tag": "conflict"}}}')
        with self.assertRaises(RuntimeError) as ctx:
            self.run_action(transport)
        self.assertIn("409", str(ctx.exception))
        self.assertIn("path/conflict", str(ctx.exception))

    def test_raises_when_unreachable(self):
        def transport(method, url, *, headers, body, timeout=15):
            raise OSError("no network")

        with self.assertRaises(RuntimeError) as ctx:
            self.run_action(transport)
        self.assertIn("unreachable", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
