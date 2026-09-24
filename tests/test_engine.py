import json
import unittest
from copy import deepcopy
from unittest.mock import MagicMock, patch

from src.engine import matches, run_dataops, run_dropbox_delete, run_dropbox_upload, run_slack


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

    def test_names_rendered_output_after_the_artifact_key(self):
        transport = FakeTransport()
        action = {
            "type": "dropbox_upload", "connection_id": "dropbox",
            "source": "output", "folder": "/Invoices",
        }
        event = {"data": {"output": {"bucket": "artifacts", "key": "rendered/1.pdf"}}}
        self.run_action(transport, event=event, action=action)

        arg = json.loads(transport.calls[0]["headers"]["dropbox-api-arg"])
        self.assertEqual(arg["path"], "/Invoices/1.pdf")

    def test_makes_relative_folder_absolute(self):
        transport = FakeTransport()
        action = {
            "type": "dropbox_upload", "connection_id": "dropbox",
            "folder": "_dtc_paperwork/income-invoices",
        }
        self.run_action(transport, action=action)

        arg = json.loads(transport.calls[0]["headers"]["dropbox-api-arg"])
        self.assertEqual(arg["path"], "/_dtc_paperwork/income-invoices/invoice.pdf")

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


class DropboxDeleteTests(unittest.TestCase):
    def setUp(self):
        self.connection = {"connection_id": "dropbox", "provider": "dropbox", "status": "connected"}
        self.event = {"connector": "dropbox", "event": "file.created",
                      "data": {"path": "/_dtc_paperwork/income-invoices/1.pdf"}}

    def run_action(self, transport, event=None, action=None):
        action = action or {"type": "dropbox_delete", "connection_id": "dropbox"}
        with patch("src.engine._dropbox_connection", return_value=dict(self.connection)), \
             patch("src.tokens.get_access_token", return_value=("token-123", {})):
            run_dropbox_delete(action, event or dict(self.event), transport=transport)

    def test_deletes_the_event_path(self):
        transport = FakeTransport()

        self.run_action(transport)

        call = transport.calls[0]
        self.assertEqual(call["method"], "POST")
        self.assertEqual(call["url"], "https://api.dropboxapi.com/2/files/delete_v2")
        self.assertEqual(call["headers"]["authorization"], "Bearer token-123")
        self.assertEqual(json.loads(call["body"]), {"path": "/_dtc_paperwork/income-invoices/1.pdf"})

    def test_prefers_explicit_path_over_event(self):
        transport = FakeTransport()
        action = {"type": "dropbox_delete", "connection_id": "dropbox", "path": "/other.pdf"}

        self.run_action(transport, action=action)

        self.assertEqual(json.loads(transport.calls[0]["body"]), {"path": "/other.pdf"})

    def test_fails_without_a_path(self):
        with self.assertRaises(ValueError):
            self.run_action(FakeTransport(), event={"data": {}})

    def test_raises_on_dropbox_error(self):
        transport = FakeTransport(status=409, body=b'{"error": {".tag": "path_lookup", "path_lookup": {".tag": "not_found"}}}')

        with self.assertRaises(RuntimeError) as ctx:
            self.run_action(transport)
        self.assertIn("409", str(ctx.exception))
        self.assertIn("path_lookup/not_found", str(ctx.exception))

    def test_raises_when_unreachable(self):
        def transport(method, url, *, headers, body, timeout=15):
            raise OSError("no network")

        with self.assertRaises(RuntimeError) as ctx:
            self.run_action(transport)
        self.assertIn("unreachable", str(ctx.exception))


class DropboxIntakeTests(unittest.TestCase):
    def setUp(self):
        self.connection = {"connection_id": "dropbox", "provider": "dropbox", "status": "connected"}
        self.event = {
            "connector": "dropbox",
            "event": "file.created",
            "id": "dropbox:acct1:fid:rev1",
            "occurred_at": "2026-09-24T15:00:00+00:00",
            "data": {
                "path": "/_dtc_paperwork/income-invoices/1.pdf",
                "content_hash": "abc123",
            },
        }

    def run_dataops_action(self, event=None):
        action = {"type": "dataops", "connection_id": "dropbox",
                  "url_env": "DATAOPS_INTAKE_URL", "auth_secret_id": "dapier/dataops"}
        s3 = MagicMock()
        with patch("src.engine.secrets_value", return_value='{"token": "tok"}'), \
             patch("src.engine._json_request") as json_request, \
             patch("src.engine._dropbox_connection", return_value=dict(self.connection)), \
             patch("src.tokens.get_access_token", return_value=("token-123", {})), \
             patch("src.engine._dropbox_download", return_value=b"pdf-bytes"), \
             patch("boto3.client", return_value=s3), \
             patch.dict("os.environ", {"RENDER_ARTIFACTS_BUCKET": "artifacts",
                                       "DATAOPS_INTAKE_URL": "https://intake.test"}):
            run_dataops(action, event or deepcopy(self.event))
        return json_request.call_args.args[1], s3

    def test_intakes_a_copy_of_the_file(self):
        body, s3 = self.run_dataops_action()

        key = s3.put_object.call_args.kwargs["Key"]
        self.assertEqual(key, "dropbox/dropbox:acct1:fid:rev1/1.pdf")
        self.assertTrue(key.startswith("dropbox/"))
        doc = body["documents"][0]
        self.assertEqual(doc["storageUri"], f"s3://artifacts/{key}")
        self.assertEqual(doc["filename"], "1.pdf")
        self.assertEqual(doc["contentType"], "application/pdf")
        self.assertEqual(doc["sizeBytes"], len(b"pdf-bytes"))
        self.assertEqual(doc["checksum"], "sha256:abc123")

    def test_uses_deterministic_event_id_as_message_id(self):
        body, _s3 = self.run_dataops_action()

        self.assertEqual(body["messageId"], "dropbox:acct1:fid:rev1")
        self.assertEqual(body["subject"], "1.pdf")
        self.assertEqual(body["receivedAt"], "2026-09-24T15:00:00+00:00")

    def test_fails_without_a_path(self):
        with self.assertRaises(ValueError):
            self.run_dataops_action(event={"connector": "dropbox", "event": "file.created",
                                           "id": "x", "occurred_at": "now", "data": {}})


if __name__ == "__main__":
    unittest.main()
