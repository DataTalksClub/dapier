"""dropbox_read_file: download through the connection, stage the bytes for
the steps that follow, and surface the staged ref in the step output."""
import json
import unittest
from unittest.mock import patch

from src.dapier.connectors.registry import (
    action_specs,
    run_action,
    validate_action_chain,
)
from plugins.dropbox.runners.dropbox import run_dropbox_read_file


class FakeTransport:
    def __init__(self, status=200, body=b"pdf-bytes"):
        self.status = status
        self.body = body
        self.calls = []

    def __call__(self, method, url, *, headers, body, timeout=15):
        self.calls.append({"method": method, "url": url, "headers": headers, "body": body})
        return self.status, self.body


class FakeS3:
    def __init__(self):
        self.objects = {}

    def put_object(self, Bucket, Key, Body, **kwargs):
        self.objects[(Bucket, Key)] = (Body, kwargs)


CONNECTION = {"connection_id": "dropbox", "provider": "dropbox", "status": "connected"}
EVENT = {"id": "evt/1", "connector": "dropbox", "event": "file.created",
         "data": {"path": "/Invoices/invoice-4137.pdf"}}


class DropboxReadFileTests(unittest.TestCase):
    def run_read(self, transport, action=None, event=None, s3_client=None):
        action = action or {"type": "dropbox_read_file", "connection_id": "dropbox"}
        with patch("plugins.dropbox.runners.dropbox._dropbox_connection",
                   return_value=dict(CONNECTION)), \
             patch("src.dapier.connections.tokens.get_access_token",
                   return_value=("token-123", {})), \
             patch.dict("os.environ", {"RENDER_ARTIFACTS_BUCKET": "artifacts"}):
            return run_dropbox_read_file(action, event or dict(EVENT),
                                         transport=transport, s3_client=s3_client)

    def test_downloads_the_event_path_and_stages_it(self):
        transport = FakeTransport()
        s3 = FakeS3()

        output = self.run_read(transport, s3_client=s3)

        call = transport.calls[0]
        self.assertEqual(call["url"], "https://content.dropboxapi.com/2/files/download")
        self.assertEqual(call["headers"]["authorization"], "Bearer token-123")
        self.assertEqual(json.loads(call["headers"]["dropbox-api-arg"]),
                         {"path": "/Invoices/invoice-4137.pdf"})
        (body, kwargs), = s3.objects.values()
        self.assertEqual(body, b"pdf-bytes")
        self.assertEqual(kwargs["ContentType"], "application/pdf")
        self.assertEqual(kwargs["ServerSideEncryption"], "AES256")
        self.assertEqual(output, {
            "filename": "invoice-4137.pdf",
            "size": 9,
            "content_type": "application/pdf",
            "bucket": "artifacts",
            "key": "dropbox/evt_1/read/invoice-4137.pdf",
            "path": "/Invoices/invoice-4137.pdf",
        })

    def test_explicit_path_templates_and_wins_over_the_event(self):
        transport = FakeTransport()
        s3 = FakeS3()
        action = {"type": "dropbox_read_file", "connection_id": "dropbox",
                  "path": "/Export/{name}"}
        event = {"id": "e2", "data": {"name": "a.csv", "path": "/ignore-me"}}

        output = self.run_read(transport, action=action, event=event, s3_client=s3)

        arg = json.loads(transport.calls[0]["headers"]["dropbox-api-arg"])
        self.assertEqual(arg, {"path": "/Export/a.csv"})
        self.assertEqual(output["filename"], "a.csv")
        self.assertEqual(output["content_type"], "text/csv")
        self.assertEqual(output["key"], "dropbox/e2/read/a.csv")

    def test_fails_without_any_path(self):
        with self.assertRaises(ValueError):
            self.run_read(FakeTransport(), event={"data": {}})

    def test_download_error_surfaces_as_runtime_error(self):
        with self.assertRaises(RuntimeError):
            self.run_read(FakeTransport(status=409, body=b'{"error": {".tag": "path_lookup"}}'))

    def test_dispatches_through_the_registry(self):
        transport = FakeTransport()
        s3 = FakeS3()
        with patch("src.dapier.engine.actions.base._default_transport", transport), \
             patch("plugins.dropbox.runners.dropbox._dropbox_connection",
                   return_value=dict(CONNECTION)), \
             patch("src.dapier.connections.tokens.get_access_token",
                   return_value=("token-123", {})), \
             patch("boto3.client", return_value=s3), \
             patch.dict("os.environ", {"RENDER_ARTIFACTS_BUCKET": "artifacts"}):
            output = run_action(
                {"type": "dropbox_read_file", "connection_id": "dropbox", "id": "fetch"},
                dict(EVENT), None, steps={})

        self.assertEqual(output["key"], "dropbox/evt_1/fetch/invoice-4137.pdf")
        self.assertIn(("artifacts", output["key"]), s3.objects)

    def test_registry_reports_the_runnable_signature(self):
        required, optional = action_specs()["dropbox_read_file"]
        self.assertEqual(required, frozenset({"connection_id"}))
        self.assertEqual(optional, frozenset({"path"}))
        validate_action_chain([{"type": "dropbox_read_file", "connection_id": "dropbox",
                                "path": "/Invoices/invoice-4137.pdf"}])


if __name__ == "__main__":
    unittest.main()
