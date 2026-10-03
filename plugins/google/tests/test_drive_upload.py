"""drive_upload_file: one file into the connection's Drive via the v3
multipart upload, with the s3_upload source composition (source_url /
source_s3 / inline content — exactly one).

Unit tests drive the registered runner with a fake transport and the
connection/token seams patched (the test_find_s3_drive DriveFindTests
pattern); the registry half checks the spec, chain validation and engine
dispatch.
"""
import json
import unittest
from unittest.mock import patch

from src.dapier.connectors import registry
from plugins.google.runners.drive import run_drive_upload_file

UPLOAD_URL_PREFIX = ("https://www.googleapis.com/upload/drive/v3/files"
                     "?uploadType=multipart&supportsAllDrives=true")

UPLOAD_RESPONSE = {
    "id": "1a2B3c4D5e6F",
    "name": "report.pdf",
    "mimeType": "application/pdf",
    "size": "81244",
    "webViewLink": "https://drive.google.com/file/d/1a2B3c4D5e6F/view",
}

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


def run_upload(transport, action, event=None, *, staged_bytes=b"staged-bytes"):
    action = {"type": "drive_upload_file", "connection_id": "google", **action}
    with patch("src.dapier.engine.actions.base._connected_connection",
               return_value=dict(GOOGLE_CONNECTION)), \
         patch("src.dapier.connections.tokens.get_access_token",
               return_value=("tok", {})), \
         patch("src.dapier.engine.actions.base._s3_body",
               return_value=staged_bytes) as s3_body:
        output = run_drive_upload_file(action, event or {"data": {}}, steps={},
                                       transport=transport)
    return output, s3_body


def multipart_parts(call):
    """The multipart body split into (json head, metadata, media head, media)."""
    boundary = call["headers"]["content-type"].split("boundary=")[1].encode()
    parts = call["body"].split(b"--" + boundary)
    json_head, metadata_raw = parts[1].split(b"\r\n\r\n", 1)
    media_head, media = parts[2].split(b"\r\n\r\n", 1)
    return json_head, json.loads(metadata_raw.rsplit(b"\r\n", 1)[0]), media_head, media


class DriveUploadTests(unittest.TestCase):
    def test_inline_content_uploads_multipart(self):
        transport = FakeTransport(("uploadType=multipart", 200,
                                   json_body(UPLOAD_RESPONSE)))

        output, _s3 = run_upload(transport, {"name": "report.pdf",
                                             "content": "hello world",
                                             "content_type": "text/plain"})

        self.assertEqual(output, {
            "file_id": "1a2B3c4D5e6F", "name": "report.pdf",
            "mime_type": "application/pdf", "size": "81244",
            "webViewLink": "https://drive.google.com/file/d/1a2B3c4D5e6F/view"})
        self.assertEqual(len(transport.calls), 1)
        call = transport.calls[0]
        self.assertEqual(call["method"], "POST")
        self.assertTrue(call["url"].startswith(UPLOAD_URL_PREFIX))
        self.assertIn("fields=id%2Cname%2CmimeType%2Csize%2CwebViewLink", call["url"])
        self.assertEqual(call["headers"]["authorization"], "Bearer tok")
        self.assertTrue(call["headers"]["content-type"]
                        .startswith("multipart/related; boundary="))
        json_head, metadata, media_head, media = multipart_parts(call)
        self.assertEqual(metadata, {"name": "report.pdf"})
        self.assertIn(b"Content-Type: application/json; charset=UTF-8", json_head)
        self.assertIn(b"Content-Type: text/plain", media_head)
        self.assertEqual(media, b"hello world\r\n")

    def test_source_url_downloads_then_uploads_the_bytes(self):
        transport = FakeTransport(
            ("example.test/report", 200, b"%PDF-1.4 bytes"),
            ("uploadType=multipart", 200, json_body(UPLOAD_RESPONSE)))

        output, _s3 = run_upload(transport, {
            "name": "report.pdf",
            "source_url": "https://example.test/report"})

        self.assertEqual(output["file_id"], "1a2B3c4D5e6F")
        self.assertEqual([call["method"] for call in transport.calls],
                         ["GET", "POST"])
        _json_head, _metadata, _media_head, media = multipart_parts(transport.calls[1])
        self.assertEqual(media, b"%PDF-1.4 bytes\r\n")

    def test_source_s3_uploads_the_staged_object(self):
        transport = FakeTransport(("uploadType=multipart", 200,
                                   json_body(UPLOAD_RESPONSE)))

        _output, s3_body = run_upload(
            transport, {"name": "report.pdf",
                        "source_s3": {"bucket": "staging",
                                      "key": "invoices/report.pdf"}})

        s3_body.assert_called_once_with(
            {"bucket": "staging", "key": "invoices/report.pdf"})
        self.assertEqual(len(transport.calls), 1)  # no HTTP download
        _json_head, _metadata, _media_head, media = multipart_parts(transport.calls[0])
        self.assertEqual(media, b"staged-bytes\r\n")

    def test_content_source_takes_templates(self):
        transport = FakeTransport(("uploadType=multipart", 200,
                                   json_body(UPLOAD_RESPONSE)))

        run_upload(transport, {"name": "{filename}", "content": "{text}"},
                   event={"data": {"filename": "notes.txt",
                                   "text": "rendered body"}})

        _json_head, metadata, _media_head, media = multipart_parts(transport.calls[0])
        self.assertEqual(metadata, {"name": "notes.txt"})
        self.assertEqual(media, b"rendered body\r\n")

    def test_folder_id_lands_in_the_metadata_parents(self):
        transport = FakeTransport(("uploadType=multipart", 200,
                                   json_body(UPLOAD_RESPONSE)))

        run_upload(transport, {"name": "report.pdf", "content": "x",
                               "folder_id": "fold-9"})

        _json_head, metadata, _media_head, _media = multipart_parts(transport.calls[0])
        self.assertEqual(metadata, {"name": "report.pdf",
                                    "parents": ["fold-9"]})

    def test_without_a_folder_the_metadata_has_no_parents(self):
        transport = FakeTransport(("uploadType=multipart", 200,
                                   json_body(UPLOAD_RESPONSE)))

        run_upload(transport, {"name": "report.pdf", "content": "x",
                               "folder_id": "{data.missing}"})

        _json_head, metadata, _media_head, _media = multipart_parts(transport.calls[0])
        self.assertEqual(metadata, {"name": "report.pdf"})

    def test_content_type_defaults_to_octet_stream(self):
        transport = FakeTransport(("uploadType=multipart", 200,
                                   json_body(UPLOAD_RESPONSE)))

        run_upload(transport, {"name": "report.pdf", "content": "x"})

        _json_head, _metadata, media_head, _media = multipart_parts(transport.calls[0])
        self.assertIn(b"Content-Type: application/octet-stream", media_head)

    def test_two_sources_conflict_without_any_call(self):
        transport = FakeTransport()

        for action in (
            {"source_url": "https://example.test/f", "content": "inline"},
            {"source_url": "https://example.test/f",
             "source_s3": {"bucket": "b", "key": "k"}},
            {"content": "inline", "source_s3": {"bucket": "b", "key": "k"}},
        ):
            with self.assertRaises(ValueError) as caught:
                run_upload(transport, {"name": "report.pdf", **action})
            self.assertIn("one content source", str(caught.exception))
        self.assertEqual(transport.calls, [])

    def test_no_source_fails(self):
        transport = FakeTransport()

        with self.assertRaises(ValueError) as caught:
            run_upload(transport, {"name": "report.pdf"})
        self.assertIn("needs source_url", str(caught.exception))
        # a half-given source_s3 lands on the same message
        with self.assertRaises(ValueError):
            run_upload(transport, {"name": "report.pdf",
                                   "source_s3": {"bucket": "b"}})
        self.assertEqual(transport.calls, [])

    def test_missing_name_fails_before_any_call(self):
        transport = FakeTransport()

        with self.assertRaises(ValueError) as caught:
            run_upload(transport, {"content": "x", "name": "  "})
        self.assertIn("requires a name", str(caught.exception))
        self.assertEqual(transport.calls, [])

    def test_upload_http_error_surfaces_status_and_detail(self):
        transport = FakeTransport(
            ("uploadType=multipart", 403,
             json_body({"error": {"message": "no drive scope"}})))

        with self.assertRaises(RuntimeError) as caught:
            run_upload(transport, {"name": "report.pdf", "content": "x"})
        self.assertIn("HTTP 403", str(caught.exception))
        self.assertIn("no drive scope", str(caught.exception))

    def test_source_url_download_error_stops_before_the_upload(self):
        transport = FakeTransport(("example.test/report", 404, b"missing"),
                                  ("uploadType=multipart", 200,
                                   json_body(UPLOAD_RESPONSE)))

        with self.assertRaises(RuntimeError) as caught:
            run_upload(transport, {"name": "report.pdf",
                                   "source_url": "https://example.test/report"})
        self.assertIn("file download returned HTTP 404", str(caught.exception))
        self.assertEqual(len(transport.calls), 1)

    def test_unreachable_transport_maps_to_a_runtime_error(self):
        def broken(method, url, *, headers=None, body=None, timeout=15):
            raise ConnectionError("no route to host")

        with patch("src.dapier.engine.actions.base._connected_connection",
                   return_value=dict(GOOGLE_CONNECTION)), \
             patch("src.dapier.connections.tokens.get_access_token",
                   return_value=("tok", {})):
            with self.assertRaises(RuntimeError) as caught:
                run_drive_upload_file(
                    {"type": "drive_upload_file", "connection_id": "google",
                     "name": "report.pdf", "content": "x"},
                    {"data": {}}, transport=broken)
        self.assertIn("google drive unreachable", str(caught.exception))
        self.assertIn("ConnectionError", str(caught.exception))

    def test_unreadable_response_yields_null_output_values(self):
        def junk(method, url, *, headers=None, body=None, timeout=15):
            return 200, b"<html>not json</html>"

        with patch("src.dapier.engine.actions.base._connected_connection",
                   return_value=dict(GOOGLE_CONNECTION)), \
             patch("src.dapier.connections.tokens.get_access_token",
                   return_value=("tok", {})):
            output = run_drive_upload_file(
                {"type": "drive_upload_file", "connection_id": "google",
                 "name": "report.pdf", "content": "x"},
                {"data": {}}, transport=junk)

        self.assertEqual(sorted(output),
                         ["file_id", "mime_type", "name", "size", "webViewLink"])
        self.assertIsNone(output["file_id"])


class RegistryTests(unittest.TestCase):
    """The registry entry is what the engine and trigger validation use."""

    def test_the_action_is_registered_with_its_field_spec(self):
        self.assertIn("drive_upload_file", registry.ACTIONS)
        self.assertEqual(registry.action_specs()["drive_upload_file"],
                         ({"connection_id", "name"},
                          {"source_url", "source_s3", "content",
                           "folder_id", "content_type"}))
        folder_field = next(field for field
                            in registry.ACTIONS["drive_upload_file"].fields
                            if field["key"] == "folder_id")
        self.assertEqual(folder_field.get("discover"),
                         {"resource": "google-drive.folders"})

    def test_a_chain_using_the_action_validates(self):
        registry.validate_action_chain([
            {"type": "drive_upload_file", "connection_id": "google",
             "name": "report.pdf", "content": "{subject}"},
            {"type": "drive_upload_file", "connection_id": "google",
             "name": "report.pdf",
             "source_s3": {"bucket": "staging", "key": "{steps.read.output.key}"},
             "folder_id": "{steps.find.output.folder}"},
        ])

    def test_validation_rejects_missing_and_unknown_keys(self):
        with self.assertRaises(registry.ActionError) as caught:
            registry.validate_action_chain([
                {"type": "drive_upload_file", "connection_id": "google",
                 "content": "x"}])
        self.assertIn("missing: name", str(caught.exception))
        with self.assertRaises(registry.ActionError) as caught:
            registry.validate_action_chain([
                {"type": "drive_upload_file", "connection_id": "google",
                 "name": "f", "content": "x", "parents": ["fold"]}] )
        self.assertIn("unknown keys: parents", str(caught.exception))

    def test_a_url_typed_source_url_rejects_a_non_url_literal(self):
        with self.assertRaises(registry.ActionError) as caught:
            registry.validate_action_chain([
                {"type": "drive_upload_file", "connection_id": "google",
                 "name": "f", "source_url": "not a url"}])
        self.assertIn("source_url", str(caught.exception))
        # a template stays legal — what it renders to is data, not config
        registry.validate_action_chain([
            {"type": "drive_upload_file", "connection_id": "google",
             "name": "f", "source_url": "{steps.link.output.link}"}])

    def test_engine_dispatch_runs_the_action_end_to_end(self):
        transport = FakeTransport(("uploadType=multipart", 200,
                                   json_body(UPLOAD_RESPONSE)))
        with patch("src.dapier.engine.actions.base._connected_connection",
                   return_value=dict(GOOGLE_CONNECTION)), \
             patch("src.dapier.engine.actions.base._default_transport", transport), \
             patch("src.dapier.connections.tokens.get_access_token",
                   return_value=("tok", {})):
            output = registry.run_action(
                {"type": "drive_upload_file", "connection_id": "google",
                 "name": "report.pdf", "content": "hello"},
                {"data": {}}, "wf-1")

        self.assertEqual(output["file_id"], "1a2B3c4D5e6F")
        self.assertEqual(output["webViewLink"],
                         "https://drive.google.com/file/d/1a2B3c4D5e6F/view")


if __name__ == "__main__":
    unittest.main()
