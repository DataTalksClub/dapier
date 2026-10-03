"""drive_read_file: download through the connection (or export a
Google-native doc first) and stage the bytes for the steps that follow —
the drive counterpart of dropbox_read_file.

Unit tests drive the registered runner with a URL-routed fake transport
(the test_drive_upload pattern); the connection/token seams and the
artifacts bucket are patched, the S3 client faked.
"""
import json
import unittest
from unittest.mock import patch

from src.dapier.connectors import registry
from plugins.google.connector import drive as drive_connector
from plugins.google.runners.drive import run_drive_read_file

FILES_URL = "https://www.googleapis.com/drive/v3/files"

GOOGLE_CONNECTION = {"connection_id": "google", "provider": "google",
                     "status": "connected", "credential_id": "oauth#google"}


def json_body(payload):
    return json.dumps(payload).encode()


class FakeTransport:
    """Records every call; routes by URL substring to (status, body bytes)."""

    def __init__(self, *routes):
        self.routes = routes  # (substring, status, body) tuples
        self.calls = []

    def __call__(self, method, url, *, headers=None, body=None, timeout=15):
        self.calls.append({"method": method, "url": url, "headers": headers,
                           "body": body, "timeout": timeout})
        for substring, status, payload in self.routes:
            if substring in url:
                return status, payload
        raise AssertionError(f"unexpected provider call: {method} {url}")


class FakeS3:
    def __init__(self):
        self.objects = {}

    def put_object(self, Bucket, Key, Body, **kwargs):
        self.objects[(Bucket, Key)] = (Body, kwargs)


def run_read(transport, action=None, event=None, s3_client=None):
    action = {"type": "drive_read_file", "connection_id": "google",
              "file_id": "FILE0123", **(action or {})}
    with patch("src.dapier.engine.actions.base._connected_connection",
               return_value=dict(GOOGLE_CONNECTION)), \
         patch("src.dapier.connections.tokens.get_access_token",
               return_value=("tok", {})), \
         patch.dict("os.environ", {"RENDER_ARTIFACTS_BUCKET": "artifacts"}):
        return run_drive_read_file(action, event or {"id": "evt/9", "data": {}},
                                   transport=transport, s3_client=s3_client)


class DriveReadFileTests(unittest.TestCase):
    def routes_binary(self, name="report.pdf", mime="application/pdf"):
        metadata = json_body({"id": "FILE0123", "name": name, "mimeType": mime})
        return (FakeTransport(("fields=id%2Cname%2CmimeType", 200, metadata),
                              ("alt=media", 200, b"pdf-bytes")))

    def test_downloads_the_file_and_stages_it(self):
        transport = self.routes_binary()
        s3 = FakeS3()

        output = run_read(transport, s3_client=s3)

        metadata_call, content_call = transport.calls[0], transport.calls[1]
        self.assertTrue(metadata_call["url"].startswith(FILES_URL + "/FILE0123?"))
        self.assertIn("fields=id%2Cname%2CmimeType", metadata_call["url"])
        self.assertEqual(metadata_call["headers"]["authorization"], "Bearer tok")
        self.assertIn("alt=media", content_call["url"])
        self.assertIn("supportsAllDrives=true", content_call["url"])
        (body, kwargs), = s3.objects.values()
        self.assertEqual(body, b"pdf-bytes")
        self.assertEqual(kwargs["ContentType"], "application/pdf")
        self.assertEqual(kwargs["ServerSideEncryption"], "AES256")
        self.assertEqual(output, {
            "filename": "report.pdf",
            "size": 9,
            "content_type": "application/pdf",
            "bucket": "artifacts",
            "key": "drive/evt_9/read/report.pdf",
            "file_id": "FILE0123",
            "name": "report.pdf",
            "mime_type": "application/pdf",
        })

    def test_templates_file_id_and_step_id_names_the_staged_key(self):
        transport = self.routes_binary()
        s3 = FakeS3()
        action = {"file_id": "{file_id}", "id": "fetch"}
        event = {"id": "e2", "data": {"file_id": "TRIG-1"}}

        output = run_read(transport, action, event, s3_client=s3)

        self.assertTrue(transport.calls[0]["url"].startswith(FILES_URL + "/TRIG-1?"))
        self.assertEqual(output["key"], "drive/e2/fetch/report.pdf")
        self.assertEqual(output["file_id"], "TRIG-1")

    def test_event_data_id_is_the_last_fallback(self):
        transport = self.routes_binary()
        run_read(transport, {"file_id": ""}, {"id": "e3", "data": {"id": "EVTID"}},
                 s3_client=FakeS3())
        self.assertTrue(transport.calls[0]["url"].startswith(FILES_URL + "/EVTID?"))

    def test_google_native_document_exports_as_pdf_by_default(self):
        transport = FakeTransport(
            ("fields=id%2Cname%2CmimeType", 200,
             json_body({"id": "DOC1", "name": "Q3 Notes",
                        "mimeType": "application/vnd.google-apps.document"})),
            ("files/DOC1/export", 200, b"%PDF-exported"))
        s3 = FakeS3()

        output = run_read(transport, {"file_id": "DOC1"}, s3_client=s3)

        export_call = transport.calls[1]
        self.assertIn("/export?", export_call["url"])
        self.assertIn("mimeType=application%2Fpdf", export_call["url"])
        (body, kwargs), = s3.objects.values()
        self.assertEqual(body, b"%PDF-exported")
        self.assertEqual(kwargs["ContentType"], "application/pdf")
        self.assertEqual(output["filename"], "Q3 Notes")
        self.assertEqual(output["content_type"], "application/pdf")
        self.assertEqual(output["mime_type"], "application/vnd.google-apps.document")

    def test_explicit_export_as_wins_and_unknown_native_needs_one(self):
        transport = FakeTransport(
            ("fields=id%2Cname%2CmimeType", 200,
             json_body({"id": "S1", "name": "tracker",
                        "mimeType": "application/vnd.google-apps.spreadsheet"})),
            ("files/S1/export", 200, b"a,b\n1,2"))
        output = run_read(transport, {"file_id": "S1", "export_as": "text/csv"},
                          s3_client=FakeS3())
        self.assertIn("mimeType=text%2Fcsv", transport.calls[1]["url"])
        self.assertEqual(output["content_type"], "text/csv")

        native = FakeTransport(
            ("fields=id%2Cname%2CmimeType", 200,
             json_body({"id": "F1", "name": "sketch",
                        "mimeType": "application/vnd.google-apps.form"})))
        with self.assertRaisesRegex(ValueError, "export_as"):
            run_read(native, {"file_id": "F1"}, s3_client=FakeS3())

    def test_drive_errors_surface_as_runtime_errors(self):
        transport = FakeTransport(("fields=id%2Cname%2CmimeType", 404, b"{}"))
        with self.assertRaisesRegex(RuntimeError, "HTTP 404"):
            run_read(transport, s3_client=FakeS3())

    def test_fails_without_any_file_id(self):
        with self.assertRaises(ValueError):
            run_read(self.routes_binary(), {"file_id": ""},
                     {"id": "e4", "data": {}}, s3_client=FakeS3())

    def test_dispatches_through_the_registry(self):
        from src.dapier.connectors.registry import run_action

        transport = self.routes_binary()
        s3 = FakeS3()
        with patch("src.dapier.engine.actions.base._default_transport", transport), \
             patch("src.dapier.engine.actions.base._connected_connection",
                   return_value=dict(GOOGLE_CONNECTION)), \
             patch("src.dapier.connections.tokens.get_access_token",
                   return_value=("tok", {})), \
             patch("boto3.client", return_value=s3), \
             patch.dict("os.environ", {"RENDER_ARTIFACTS_BUCKET": "artifacts"}):
            output = run_action(
                {"type": "drive_read_file", "connection_id": "google",
                 "file_id": "FILE0123", "id": "fetch"},
                {"id": "e5", "data": {}}, None, steps={})

        self.assertEqual(output["key"], "drive/e5/fetch/report.pdf")
        self.assertIn(("artifacts", output["key"]), s3.objects)

    def test_registry_reports_the_runnable_signature(self):
        from src.dapier.connectors.registry import action_specs, validate_action_chain

        required, optional = action_specs()["drive_read_file"]
        self.assertEqual(required, frozenset({"connection_id", "file_id"}))
        self.assertEqual(optional, frozenset({"export_as"}))
        validate_action_chain([{"type": "drive_read_file", "connection_id": "google",
                                "file_id": "FILE0123"}])


if __name__ == "__main__":
    unittest.main()
