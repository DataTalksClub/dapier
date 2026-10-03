"""youtube_upload_video: one video onto the connection's channel via the
Data API multipart upload, with the s3_upload source composition (source_url
/ source_s3 / inline content — exactly one) and the in-memory size cap.

Unit tests drive the registered runner with a fake transport and the
connection/token seams patched (the test_drive_upload pattern); the registry
half checks the spec, chain validation and engine dispatch.
"""
import json
import unittest
from unittest.mock import patch

from src.dapier.connectors import registry
from plugins.google.runners.youtube import run_youtube_upload_video

UPLOAD_URL_PREFIX = ("https://www.googleapis.com/upload/youtube/v3/videos"
                     "?uploadType=multipart&part=snippet%2Cstatus")

UPLOAD_RESPONSE = {
    "id": "dQw4w9WgXcQ",
    "snippet": {"title": "Deploying dapier: a walkthrough"},
    "status": {"privacyStatus": "unlisted", "uploadStatus": "uploaded"},
}

YOUTUBE_CONNECTION = {"connection_id": "youtube", "provider": "youtube",
                      "status": "connected", "credential_id": "oauth#youtube"}


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
    action = {"type": "youtube_upload_video", "connection_id": "youtube", **action}
    with patch("src.dapier.engine.actions.base._connected_connection",
               return_value=dict(YOUTUBE_CONNECTION)), \
         patch("src.dapier.connections.tokens.get_access_token",
               return_value=("tok", {})), \
         patch("src.dapier.engine.actions.base._s3_body",
               return_value=staged_bytes) as s3_body:
        output = run_youtube_upload_video(action, event or {"data": {}}, steps={},
                                          transport=transport)
    return output, s3_body


def multipart_parts(call):
    """The multipart body split into (json head, metadata, media head, media)."""
    boundary = call["headers"]["content-type"].split("boundary=")[1].encode()
    parts = call["body"].split(b"--" + boundary)
    json_head, metadata_raw = parts[1].split(b"\r\n\r\n", 1)
    media_head, media = parts[2].split(b"\r\n\r\n", 1)
    return json_head, json.loads(metadata_raw.rsplit(b"\r\n", 1)[0]), media_head, media


class YoutubeUploadTests(unittest.TestCase):
    def test_inline_content_uploads_multipart(self):
        transport = FakeTransport(("uploadType=multipart", 200,
                                   json_body(UPLOAD_RESPONSE)))

        output, _s3 = run_upload(transport, {"title": "Deploying dapier",
                                             "content": "hello world"})

        self.assertEqual(output, {
            "video_id": "dQw4w9WgXcQ",
            "title": "Deploying dapier: a walkthrough",
            "privacy_status": "unlisted",
            "upload_status": "uploaded",
            "url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ"})
        self.assertEqual(len(transport.calls), 1)
        call = transport.calls[0]
        self.assertEqual(call["method"], "POST")
        self.assertTrue(call["url"].startswith(UPLOAD_URL_PREFIX))
        self.assertEqual(call["headers"]["authorization"], "Bearer tok")
        self.assertTrue(call["headers"]["content-type"]
                        .startswith("multipart/related; boundary="))
        json_head, metadata, media_head, media = multipart_parts(call)
        self.assertEqual(metadata, {"snippet": {"title": "Deploying dapier"},
                                    "status": {"privacyStatus": "unlisted"}})
        self.assertIn(b"Content-Type: application/json; charset=UTF-8", json_head)
        self.assertIn(b"Content-Type: video/mp4", media_head)
        self.assertEqual(media, b"hello world\r\n")

    def test_source_url_downloads_then_uploads_the_bytes(self):
        transport = FakeTransport(
            ("example.test/talk", 200, b"fake mp4 bytes"),
            ("uploadType=multipart", 200, json_body(UPLOAD_RESPONSE)))

        output, _s3 = run_upload(transport, {
            "title": "Deploying dapier",
            "source_url": "https://example.test/talk.mp4"})

        self.assertEqual(output["video_id"], "dQw4w9WgXcQ")
        self.assertEqual([call["method"] for call in transport.calls],
                         ["GET", "POST"])
        _json_head, _metadata, _media_head, media = multipart_parts(transport.calls[1])
        self.assertEqual(media, b"fake mp4 bytes\r\n")

    def test_source_s3_uploads_the_staged_object(self):
        transport = FakeTransport(("uploadType=multipart", 200,
                                   json_body(UPLOAD_RESPONSE)))

        _output, s3_body = run_upload(
            transport, {"title": "Deploying dapier",
                        "source_s3": {"bucket": "staging",
                                      "key": "renders/talk.mp4"}})

        s3_body.assert_called_once_with(
            {"bucket": "staging", "key": "renders/talk.mp4"})
        self.assertEqual(len(transport.calls), 1)  # no HTTP download
        _json_head, _metadata, _media_head, media = multipart_parts(transport.calls[0])
        self.assertEqual(media, b"staged-bytes\r\n")

    def test_content_source_takes_templates(self):
        transport = FakeTransport(("uploadType=multipart", 200,
                                   json_body(UPLOAD_RESPONSE)))

        run_upload(transport, {"title": "{video_title}", "content": "{text}"},
                   event={"data": {"video_title": "Rendered title",
                                   "text": "rendered body"}})

        _json_head, metadata, _media_head, media = multipart_parts(transport.calls[0])
        self.assertEqual(metadata["snippet"]["title"], "Rendered title")
        self.assertEqual(media, b"rendered body\r\n")

    def test_description_tags_and_category_land_in_the_snippet(self):
        transport = FakeTransport(("uploadType=multipart", 200,
                                   json_body(UPLOAD_RESPONSE)))

        run_upload(transport, {"title": "Deploying dapier", "content": "x",
                               "description": "{summary}",
                               "tags": "devops, kubernetes,,",
                               "category_id": "22"},
                   event={"data": {"summary": "rendered summary"}})

        _json_head, metadata, _media_head, _media = multipart_parts(transport.calls[0])
        self.assertEqual(metadata["snippet"], {
            "title": "Deploying dapier",
            "description": "rendered summary",
            "tags": ["devops", "kubernetes"],
            "categoryId": "22"})

    def test_without_optionals_the_snippet_carries_only_the_title(self):
        transport = FakeTransport(("uploadType=multipart", 200,
                                   json_body(UPLOAD_RESPONSE)))

        run_upload(transport, {"title": "Deploying dapier", "content": "x",
                               "description": "{data.missing}",
                               "tags": "{data.missing}",
                               "category_id": "{data.missing}"})

        _json_head, metadata, _media_head, _media = multipart_parts(transport.calls[0])
        self.assertEqual(metadata["snippet"], {"title": "Deploying dapier"})

    def test_privacy_status_overrides_the_unlisted_default(self):
        transport = FakeTransport(("uploadType=multipart", 200,
                                   json_body(UPLOAD_RESPONSE)))

        run_upload(transport, {"title": "Deploying dapier", "content": "x",
                               "privacy_status": "PUBLIC"})

        _json_head, metadata, _media_head, _media = multipart_parts(transport.calls[0])
        self.assertEqual(metadata["status"], {"privacyStatus": "public"})

    def test_unknown_privacy_status_fails_before_any_call(self):
        transport = FakeTransport()

        with self.assertRaises(ValueError) as caught:
            run_upload(transport, {"title": "Deploying dapier", "content": "x",
                                   "privacy_status": "drafts"})
        self.assertIn("privacy_status must be one of: public, unlisted, private",
                      str(caught.exception))
        self.assertEqual(transport.calls, [])

    def test_two_sources_conflict_without_any_call(self):
        transport = FakeTransport()

        for action in (
            {"source_url": "https://example.test/t.mp4", "content": "inline"},
            {"source_url": "https://example.test/t.mp4",
             "source_s3": {"bucket": "b", "key": "k"}},
            {"content": "inline", "source_s3": {"bucket": "b", "key": "k"}},
        ):
            with self.assertRaises(ValueError) as caught:
                run_upload(transport, {"title": "Deploying dapier", **action})
            self.assertIn("one content source", str(caught.exception))
        self.assertEqual(transport.calls, [])

    def test_no_source_fails(self):
        transport = FakeTransport()

        with self.assertRaises(ValueError) as caught:
            run_upload(transport, {"title": "Deploying dapier"})
        self.assertIn("needs source_url", str(caught.exception))
        # a half-given source_s3 lands on the same message
        with self.assertRaises(ValueError):
            run_upload(transport, {"title": "Deploying dapier",
                                   "source_s3": {"bucket": "b"}})
        self.assertEqual(transport.calls, [])

    def test_missing_title_fails_before_any_call(self):
        transport = FakeTransport()

        with self.assertRaises(ValueError) as caught:
            run_upload(transport, {"content": "x", "title": "  "})
        self.assertIn("requires a title", str(caught.exception))
        self.assertEqual(transport.calls, [])

    def test_upload_http_error_surfaces_status_and_detail(self):
        transport = FakeTransport(
            ("uploadType=multipart", 403,
             json_body({"error": {"message": "The request is not properly "
                                             "authorized to upload files"}})))

        with self.assertRaises(RuntimeError) as caught:
            run_upload(transport, {"title": "Deploying dapier", "content": "x"})
        self.assertIn("HTTP 403", str(caught.exception))
        self.assertIn("not properly authorized", str(caught.exception))

    def test_source_url_download_error_stops_before_the_upload(self):
        transport = FakeTransport(("example.test/talk", 404, b"missing"),
                                  ("uploadType=multipart", 200,
                                   json_body(UPLOAD_RESPONSE)))

        with self.assertRaises(RuntimeError) as caught:
            run_upload(transport, {"title": "Deploying dapier",
                                   "source_url": "https://example.test/talk.mp4"})
        self.assertIn("video download returned HTTP 404", str(caught.exception))
        self.assertEqual(len(transport.calls), 1)

    def test_oversized_video_fails_the_in_memory_guard(self):
        transport = FakeTransport()

        with patch("plugins.google.runners.youtube.MAX_VIDEO_BYTES", 16):
            for action in (
                {"content": "0123456789abcdefghij"},
                {"source_s3": {"bucket": "b", "key": "k"}},
            ):
                with self.assertRaises(ValueError) as caught:
                    run_upload(transport, {"title": "Deploying dapier", **action},
                               staged_bytes=b"0123456789abcdefghijklmnop")
                self.assertIn("capped at 16 bytes", str(caught.exception))
        self.assertEqual(transport.calls, [])

    def test_unreachable_transport_maps_to_a_runtime_error(self):
        def broken(method, url, *, headers=None, body=None, timeout=15):
            raise ConnectionError("no route to host")

        with patch("src.dapier.engine.actions.base._connected_connection",
                   return_value=dict(YOUTUBE_CONNECTION)), \
             patch("src.dapier.connections.tokens.get_access_token",
                   return_value=("tok", {})):
            with self.assertRaises(RuntimeError) as caught:
                run_youtube_upload_video(
                    {"type": "youtube_upload_video", "connection_id": "youtube",
                     "title": "Deploying dapier", "content": "x"},
                    {"data": {}}, transport=broken)
        self.assertIn("youtube upload unreachable", str(caught.exception))
        self.assertIn("ConnectionError", str(caught.exception))

    def test_unreadable_response_yields_null_output_values(self):
        def junk(method, url, *, headers=None, body=None, timeout=15):
            return 200, b"<html>not json</html>"

        with patch("src.dapier.engine.actions.base._connected_connection",
                   return_value=dict(YOUTUBE_CONNECTION)), \
             patch("src.dapier.connections.tokens.get_access_token",
                   return_value=("tok", {})):
            output = run_youtube_upload_video(
                {"type": "youtube_upload_video", "connection_id": "youtube",
                 "title": "Deploying dapier", "content": "x"},
                {"data": {}}, transport=junk)

        self.assertEqual(sorted(output),
                         ["privacy_status", "title", "upload_status", "url",
                          "video_id"])
        self.assertIsNone(output["video_id"])
        self.assertIsNone(output["url"])


class RegistryTests(unittest.TestCase):
    """The registry entry is what the engine and trigger validation use."""

    def test_the_action_is_registered_with_its_field_spec(self):
        self.assertIn("youtube_upload_video", registry.ACTIONS)
        self.assertEqual(registry.action_specs()["youtube_upload_video"],
                         ({"connection_id", "title"},
                          {"source_url", "source_s3", "content", "description",
                           "category_id", "privacy_status", "tags"}))
        entry = registry.ACTIONS["youtube_upload_video"]
        self.assertEqual(entry.label, "Upload video")
        self.assertEqual(entry.icon, "youtube")
        privacy_field = next(field for field in entry.fields
                             if field["key"] == "privacy_status")
        self.assertEqual(privacy_field.get("options"),
                         ["public", "unlisted", "private"])
        self.assertEqual(privacy_field.get("default"), "unlisted")

    def test_a_chain_using_the_action_validates(self):
        registry.validate_action_chain([
            {"type": "youtube_upload_video", "connection_id": "youtube",
             "title": "{trigger.title}", "content": "{trigger.text}"},
            {"type": "youtube_upload_video", "connection_id": "youtube",
             "title": "Deploying dapier",
             "source_s3": {"bucket": "staging",
                           "key": "{steps.render.output.key}"},
             "privacy_status": "public", "tags": "devops, kubernetes"},
        ])

    def test_validation_rejects_missing_and_unknown_keys(self):
        with self.assertRaises(registry.ActionError) as caught:
            registry.validate_action_chain([
                {"type": "youtube_upload_video", "connection_id": "youtube",
                 "content": "x"}])
        self.assertIn("missing: title", str(caught.exception))
        with self.assertRaises(registry.ActionError) as caught:
            registry.validate_action_chain([
                {"type": "youtube_upload_video", "connection_id": "youtube",
                 "title": "t", "content": "x", "snippet": {"tags": []}}])
        self.assertIn("unknown keys: snippet", str(caught.exception))

    def test_a_url_typed_source_url_rejects_a_non_url_literal(self):
        with self.assertRaises(registry.ActionError) as caught:
            registry.validate_action_chain([
                {"type": "youtube_upload_video", "connection_id": "youtube",
                 "title": "t", "source_url": "not a url"}])
        self.assertIn("source_url", str(caught.exception))
        # a template stays legal — what it renders to is data, not config
        registry.validate_action_chain([
            {"type": "youtube_upload_video", "connection_id": "youtube",
             "title": "t",
             "source_url": "{steps.upload.output.url}"}])

    def test_a_literal_privacy_status_outside_the_options_is_rejected(self):
        with self.assertRaises(registry.ActionError) as caught:
            registry.validate_action_chain([
                {"type": "youtube_upload_video", "connection_id": "youtube",
                 "title": "t", "content": "x", "privacy_status": "drafts"}])
        self.assertIn("privacy_status", str(caught.exception))
        # a template stays legal — what it renders to is data, not config
        registry.validate_action_chain([
            {"type": "youtube_upload_video", "connection_id": "youtube",
             "title": "t", "content": "x",
             "privacy_status": "{data.visibility}"}])

    def test_engine_dispatch_runs_the_action_end_to_end(self):
        transport = FakeTransport(("uploadType=multipart", 200,
                                   json_body(UPLOAD_RESPONSE)))
        with patch("src.dapier.engine.actions.base._connected_connection",
                   return_value=dict(YOUTUBE_CONNECTION)), \
             patch("src.dapier.engine.actions.base._default_transport", transport), \
             patch("src.dapier.connections.tokens.get_access_token",
                   return_value=("tok", {})):
            output = registry.run_action(
                {"type": "youtube_upload_video", "connection_id": "youtube",
                 "title": "Deploying dapier", "content": "hello"},
                {"data": {}}, "wf-1")

        self.assertEqual(output["video_id"], "dQw4w9WgXcQ")
        self.assertEqual(output["url"],
                         "https://www.youtube.com/watch?v=dQw4w9WgXcQ")


if __name__ == "__main__":
    unittest.main()
