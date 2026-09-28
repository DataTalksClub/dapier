"""slack_upload_file: one file into a channel through Slack's three-step
files.uploadV2 flow (getUploadURLExternal → binary upload →
completeUploadExternal; Zapier's Send File).

Unit tests drive the registered runner with a URL-routed fake transport
and the credential lookup patched, matching the sibling slack action tests
(test_slack_actions.py).
"""
import json
import unittest
from unittest.mock import patch

from src.dapier.connectors import registry
from src.dapier.connectors import slack as slack_connector  # noqa: F401 (registers)
from src.dapier.engine.actions.slack import run_slack_upload_file

GET_URL_URL = "https://slack.com/api/files.getUploadURLExternal"
COMPLETE_URL = "https://slack.com/api/files.completeUploadExternal"

UPLOAD_REQUESTED = {"ok": True, "upload_url": "https://files.slack.com/files-pri/T1/x",
                    "file_id": "F0UPLOAD01"}
UPLOAD_FINISHED = {"ok": True, "files": [{"id": "F0UPLOAD01", "name": "report.pdf",
                                          "title": "report.pdf",
                                          "permalink": "https://pod.slack.com/files/T1/F0UPLOAD01/report.pdf"}]}


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


def json_body(payload):
    return json.dumps(payload).encode()


def run_upload(transport, action, event=None):
    action = {"type": "slack_upload_file", "credential_id": "slack",
              "channel": "#reports", "filename": "report.pdf",
              "content": "hello world", **action}
    with patch("src.dapier.connections.credentials.get_credential",
               return_value={"token": "xoxb-test"}):
        return run_slack_upload_file(
            action, event or {"data": {"channel_id": "C1"}}, transport=transport)


def routed(requested=UPLOAD_REQUESTED, finished=UPLOAD_FINISHED, upload_status=200):
    return FakeTransport(("getUploadURLExternal", 200, json_body(requested)),
                         ("files-pri", upload_status, b"OK"),
                         ("completeUploadExternal", 200, json_body(finished)))


class SlackUploadFileTests(unittest.TestCase):
    def test_runs_the_three_step_upload_flow(self):
        transport = routed()

        output = run_upload(transport, {})

        get_call, post_call, complete_call = transport.calls
        self.assertEqual(get_call["url"], GET_URL_URL)
        self.assertEqual(json.loads(get_call["body"]),
                         {"filename": "report.pdf", "length": 11,
                          "content_type": "application/pdf"})
        self.assertEqual(get_call["headers"]["authorization"], "Bearer xoxb-test")
        self.assertEqual(post_call["url"], UPLOAD_REQUESTED["upload_url"])
        self.assertEqual(post_call["headers"]["content-type"], "application/pdf")
        self.assertEqual(post_call["body"], b"hello world")
        self.assertNotIn("authorization", post_call["headers"])
        payload = json.loads(complete_call["body"])
        self.assertEqual(payload, {"files": [{"id": "F0UPLOAD01", "title": "report.pdf"}],
                                   "channel_id": "#reports"})
        self.assertEqual(output, {
            "ok": True,
            "channel": "#reports",
            "file": {"id": "F0UPLOAD01", "name": "report.pdf",
                     "title": "report.pdf",
                     "permalink": "https://pod.slack.com/files/T1/F0UPLOAD01/report.pdf"},
        })

    def test_renders_channel_title_comment_and_thread(self):
        transport = routed()
        event = {"data": {"channel_id": "C7", "ts": "111.333", "filename": "report.pdf"}}

        output = run_upload(transport, {
            "channel": "{channel_id}", "title": "Daily {filename}",
            "initial_comment": "the {filename} landed", "thread_ts": "{ts}",
        }, event)

        payload = json.loads(transport.calls[2]["body"])
        self.assertEqual(payload["channel_id"], "C7")
        self.assertEqual(payload["files"][0]["title"], "Daily report.pdf")
        self.assertEqual(payload["initial_comment"], "the report.pdf landed")
        self.assertEqual(payload["thread_ts"], "111.333")
        self.assertEqual(output["channel"], "C7")
        # The output echoes Slack's stored file, so the canned response's title.
        self.assertEqual(output["file"]["title"], "report.pdf")

    def test_source_url_downloads_the_bytes(self):
        transport = FakeTransport(("example.com/file", 200, b"downloaded-bytes"),
                                  ("getUploadURLExternal", 200, json_body(UPLOAD_REQUESTED)),
                                  ("files-pri", 200, b"OK"),
                                  ("completeUploadExternal", 200, json_body(UPLOAD_FINISHED)))
        output = run_upload(transport, {"content": "", "source_url": "https://example.com/file"})
        self.assertEqual(transport.calls[0]["method"], "GET")
        self.assertEqual(transport.calls[0]["body"], None)
        self.assertEqual(transport.calls[2]["body"], b"downloaded-bytes")
        self.assertTrue(output["ok"])

    def test_source_s3_stages_without_literals(self):
        transport = routed()
        with patch("src.dapier.engine.actions.base._s3_body",
                   return_value=b"staged-bytes") as s3_body:
            run_upload(transport, {"content": "",
                                   "source_s3": {"bucket": "{bucket}",
                                                 "key": "{key}"}},
                       {"data": {"bucket": "artifacts", "key": "drive/e1/read/report.pdf"}})
        s3_body.assert_called_once_with({"bucket": "artifacts",
                                         "key": "drive/e1/read/report.pdf"})
        self.assertEqual(transport.calls[1]["body"], b"staged-bytes")

    def test_two_or_zero_content_sources_are_rejected(self):
        with self.assertRaisesRegex(ValueError, "one content source"):
            run_upload(routed(), {"source_url": "https://example.com/f"})
        with self.assertRaisesRegex(ValueError, "source_url, source_s3"):
            run_upload(routed(), {"content": ""})

    def test_slack_errors_raise_with_the_code(self):
        with self.assertRaisesRegex(RuntimeError, "no_file_uploaded"):
            run_upload(routed(requested={"ok": False, "error": "no_file_uploaded"}), {})
        with self.assertRaisesRegex(RuntimeError, "channel_not_found"):
            run_upload(routed(finished={"ok": False, "error": "channel_not_found"}), {})

    def test_upload_step_failures_raise(self):
        with self.assertRaisesRegex(RuntimeError, "HTTP 500"):
            run_upload(routed(upload_status=500), {})
        with self.assertRaisesRegex(RuntimeError, "no upload target"):
            run_upload(routed(requested={"ok": True, "file_id": "F1"}), {})

    def test_channel_and_filename_are_required(self):
        with self.assertRaisesRegex(ValueError, "channel"):
            run_upload(routed(), {"channel": ""})
        with self.assertRaisesRegex(ValueError, "filename"):
            run_upload(routed(), {"filename": " "})

    def test_action_is_registered_with_its_fields(self):
        self.assertIn("slack_upload_file", registry.ACTIONS)
        required, optional = registry.action_specs()["slack_upload_file"]
        self.assertEqual(required, frozenset({"channel", "filename"}))
        self.assertIn("source_s3", optional)
        self.assertIn("thread_ts", optional)


if __name__ == "__main__":
    unittest.main()
