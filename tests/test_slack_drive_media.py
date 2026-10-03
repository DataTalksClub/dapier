"""The media round: slack_upload_file's three-step upload, the staged-bytes
handoff out of drive_read_file, and zoom_find_meeting's find-or-create.

slack_upload_file pushes the bytes through files.getUploadURLExternal → the
presigned upload URL (the URL is the credential, no bearer header) →
files.completeUploadExternal, with the bytes sourced like the other upload
actions (inline content, a source_url download, or a staged source_s3
object). The zoom section covers create_if_missing — Zapier's find-or-create
on the topic path, reusing zoom_create_meeting's payload rules; found and
plain-miss verdicts stay exactly as they were.

Unit tests drive the registered runners with fake transports and the
connection/token seams patched, matching the sibling action tests
(test_find_media.py, test_dropbox_slack_actions.py, test_drive_read_file.py).
"""
import json
import unittest.mock as mock

import pytest

from src.dapier.connectors import registry
from src.dapier.connections import credentials as credentials_module
from src.dapier.connections import tokens
from src.dapier.engine.actions import base
from plugins.google.runners.drive import run_drive_read_file
from plugins.slack.runners.slack import run_slack_upload_file
from plugins.zoom.runners import run_zoom_find_meeting

SLACK_TOKEN = {"token": "xoxb-test"}
GOOGLE_CONNECTION = {"connection_id": "gdrive", "provider": "google",
                     "status": "connected", "credential_id": "oauth#gdrive"}
ZOOM_CONNECTION = {"connection_id": "zoom-main", "provider": "zoom",
                   "status": "connected", "credential_id": "oauth#zoom-main"}

EVENT = {"id": "evt/9", "connector": "schedule", "event": "schedule.fired",
         "data": {"title": "invoice-4137"}}


class RouteTransport:
    """Route provider calls by URL substring to canned responses: dict
    payloads serve as JSON, bytes serve raw. Every call is recorded."""

    def __init__(self, *routes):
        self.routes = routes  # (substring, status, payload) tuples
        self.calls = []

    def __call__(self, method, url, *, headers=None, body=None, timeout=15):
        self.calls.append({"method": method, "url": url,
                           "headers": headers or {}, "body": body})
        for substring, status, payload in self.routes:
            if substring in url:
                if isinstance(payload, (bytes, bytearray)):
                    return status, bytes(payload)
                return status, json.dumps(payload).encode()
        raise AssertionError(f"unexpected provider call: {method} {url}")


class OrderedTransport:
    """Canned responses served in call order — for providers where the find
    and the create share one URL (Zoom's /users/me/meetings)."""

    def __init__(self, *responses):
        self.responses = list(responses)  # (status, payload) tuples
        self.calls = []

    def __call__(self, method, url, *, headers=None, body=None, timeout=15):
        self.calls.append({"method": method, "url": url,
                           "headers": headers or {}, "body": body})
        if len(self.calls) > len(self.responses):
            raise AssertionError(f"unexpected extra call: {method} {url}")
        status, payload = self.responses[len(self.calls) - 1]
        return status, json.dumps(payload).encode()


class FakeS3:
    def __init__(self):
        self.objects = {}

    def put_object(self, Bucket, Key, Body, **kwargs):
        self.objects[(Bucket, Key)] = (Body, kwargs)


def run_slack(action, transport, *, steps=None):
    with mock.patch.object(credentials_module, "get_credential",
                           return_value=dict(SLACK_TOKEN)):
        return run_slack_upload_file(
            {"type": "slack_upload_file", "credential_id": "slack", **action},
            EVENT, transport=transport, steps=steps)


UPLOAD_ROUTES = (
    ("getUploadURLExternal", 200,
     {"ok": True, "upload_url": "https://files.slack.com/upload/v1secret",
      "file_id": "F1"}),
    ("upload/v1secret", 200, {}),
    ("completeUploadExternal", 200,
     {"ok": True, "files": [{"id": "F1", "name": "invoice-4137.txt",
                             "title": "invoice-4137",
                             "permalink": "https://slack.com/files/F1"}]}),
)


# --- slack_upload_file ---------------------------------------------------------


def test_upload_runs_the_three_step_flow_with_inline_content():
    transport = RouteTransport(*UPLOAD_ROUTES)

    output = run_slack({"channel": "#alerts", "filename": "{title}.txt",
                        "content": "invoice {title}",
                        "initial_comment": "Weekly invoice",
                        "thread_ts": "1759000000.000100"}, transport)

    assert output == {"ok": True, "channel": "#alerts", "file": {
        "id": "F1", "name": "invoice-4137.txt", "title": "invoice-4137",
        "permalink": "https://slack.com/files/F1"}}
    ask, put, complete = transport.calls
    assert ask["method"] == "POST"
    assert ask["url"] == "https://slack.com/api/files.getUploadURLExternal"
    assert ask["headers"]["authorization"] == "Bearer xoxb-test"
    assert json.loads(ask["body"]) == {
        "filename": "invoice-4137.txt", "length": len(b"invoice invoice-4137"),
        "content_type": "text/plain"}
    # The binary step posts the raw bytes to the presigned URL — the URL is
    # the credential, so no bearer header rides along.
    assert put["method"] == "POST"
    assert put["url"] == "https://files.slack.com/upload/v1secret"
    assert put["body"] == b"invoice invoice-4137"
    assert put["headers"]["content-type"] == "text/plain"
    assert "authorization" not in put["headers"]
    assert complete["url"] == "https://slack.com/api/files.completeUploadExternal"
    assert json.loads(complete["body"]) == {
        "files": [{"id": "F1", "title": "invoice-4137.txt"}],
        "channel_id": "#alerts",
        "initial_comment": "Weekly invoice",
        "thread_ts": "1759000000.000100"}


def test_a_source_url_download_fills_the_upload():
    transport = RouteTransport(
        ("example.test/export", 200, b"%PDF-invoice-4137"),
        ("getUploadURLExternal", 200,
         {"ok": True, "upload_url": "https://files.slack.com/upload/v1up",
          "file_id": "F2"}),
        ("upload/v1up", 200, {}),
        ("completeUploadExternal", 200,
         {"ok": True, "files": [{"id": "F2", "title": "invoice-4137.pdf"}]}))

    output = run_slack({"channel": "#alerts", "filename": "{title}.pdf",
                        "source_url": "https://example.test/export/{title}"},
                       transport)

    download, ask, put, complete = transport.calls
    assert download["method"] == "GET"
    assert download["url"] == "https://example.test/export/invoice-4137"
    assert json.loads(ask["body"])["length"] == len(b"%PDF-invoice-4137")
    assert json.loads(ask["body"])["content_type"] == "application/pdf"
    assert put["body"] == b"%PDF-invoice-4137"
    assert json.loads(complete["body"])["files"] == \
        [{"id": "F2", "title": "invoice-4137.pdf"}]
    assert output["file"]["id"] == "F2"


def test_a_failed_source_url_download_raises_before_any_slack_call():
    transport = RouteTransport(("example.test/export", 500, {"nope": True}))

    with pytest.raises(RuntimeError, match="download returned HTTP 500"):
        run_slack({"channel": "#alerts", "filename": "x.pdf",
                   "source_url": "https://example.test/export/x"}, transport)
    assert len(transport.calls) == 1  # only the download; Slack was untouched


def test_the_bytes_take_exactly_one_source():
    transport = RouteTransport()

    with pytest.raises(ValueError, match="one content source"):
        run_slack({"channel": "#alerts", "filename": "x.txt", "content": "a",
                   "source_url": "https://example.test/x"}, transport)
    with pytest.raises(ValueError, match="needs source_url"):
        run_slack({"channel": "#alerts", "filename": "x.txt"}, transport)
    assert transport.calls == []


def test_slack_rejections_raise_with_the_error_code():
    transport = RouteTransport(
        ("getUploadURLExternal", 200, {"ok": False, "error": "no_file_storage"}))

    with pytest.raises(RuntimeError, match="no_file_storage"):
        run_slack({"channel": "#alerts", "filename": "x.txt",
                   "content": "a"}, transport)

    transport = RouteTransport(*UPLOAD_ROUTES[:2],
                               ("completeUploadExternal", 200,
                                {"ok": False, "error": "internal_error"}))
    with pytest.raises(RuntimeError, match="internal_error"):
        run_slack({"channel": "#alerts", "filename": "x.txt",
                   "content": "a"}, transport)


def test_a_failed_binary_post_raises():
    transport = RouteTransport(
        ("getUploadURLExternal", 200,
         {"ok": True, "upload_url": "https://files.slack.com/upload/v1dead",
          "file_id": "F3"}),
        ("upload/v1dead", 500, {}))

    with pytest.raises(RuntimeError, match="upload returned HTTP 500"):
        run_slack({"channel": "#alerts", "filename": "x.txt",
                   "content": "a"}, transport)


def test_channel_and_filename_are_required():
    with pytest.raises(ValueError, match="channel"):
        run_slack({"filename": "x.txt", "content": "a"}, RouteTransport())
    with pytest.raises(ValueError, match="filename"):
        run_slack({"channel": "#alerts", "content": "a"}, RouteTransport())


def test_slack_upload_file_dispatches_through_the_registry():
    transport = RouteTransport(*UPLOAD_ROUTES)

    with mock.patch.object(credentials_module, "get_credential",
                           return_value=dict(SLACK_TOKEN)), \
         mock.patch.object(base, "_default_transport", transport):
        output = registry.ACTIONS["slack_upload_file"].run(
            {"type": "slack_upload_file", "credential_id": "slack",
             "channel": "#alerts", "filename": "report.txt",
             "content": "hello"}, EVENT, "wf-1")

    assert output["ok"] is True
    assert output["file"]["id"] == "F1"


# --- drive_read_file hands its staged bytes to slack_upload_file ---------------


def test_a_drive_read_stages_bytes_that_the_slack_upload_sends():
    s3 = FakeS3()
    drive_transport = RouteTransport(
        # The metadata GET and the media GET share the files/f-4137 path, so
        # route on what distinguishes them: the metadata call's fields param.
        ("fields=id%2Cname%2CmimeType", 200,
         {"id": "f-4137", "name": "invoice-4137.pdf",
          "mimeType": "application/pdf"}),
        ("alt=media", 200, b"%PDF-4137"))

    with mock.patch.object(base, "_connected_connection",
                           return_value=dict(GOOGLE_CONNECTION)), \
         mock.patch.object(tokens, "get_access_token",
                           return_value=("tok", {})), \
         mock.patch.dict("os.environ", {"RENDER_ARTIFACTS_BUCKET": "artifacts"}):
        staged = run_drive_read_file(
            {"type": "drive_read_file", "connection_id": "gdrive",
             "file_id": "f-4137", "id": "read"},
            dict(EVENT), transport=drive_transport, s3_client=s3)

    assert staged["filename"] == "invoice-4137.pdf"
    assert staged["bucket"] == "artifacts"
    assert staged["key"] == "drive/evt_9/read/invoice-4137.pdf"
    assert staged["content_type"] == "application/pdf"
    assert s3.objects[("artifacts", staged["key"])][0] == b"%PDF-4137"

    slack_transport = RouteTransport(
        ("getUploadURLExternal", 200,
         {"ok": True, "upload_url": "https://files.slack.com/upload/v1chain",
          "file_id": "F9"}),
        ("upload/v1chain", 200, {}),
        ("completeUploadExternal", 200,
         {"ok": True, "files": [{"id": "F9", "title": "invoice-4137.pdf"}]}))

    with mock.patch.object(credentials_module, "get_credential",
                           return_value=dict(SLACK_TOKEN)), \
         mock.patch.object(base, "_s3_body",
                           lambda ref: s3.objects[(ref["bucket"], ref["key"])][0]):
        sent = run_slack_upload_file(
            {"type": "slack_upload_file", "credential_id": "slack",
             "channel": "#alerts", "filename": "{title}.pdf", "id": "send",
             "source_s3": {"bucket": "{steps.read.output.bucket}",
                           "key": "{steps.read.output.key}"}},
            dict(EVENT), steps={"read": {"output": staged}},
            transport=slack_transport)

    assert sent["ok"] is True
    assert sent["file"]["id"] == "F9"
    upload = slack_transport.calls[1]
    assert upload["body"] == b"%PDF-4137"  # the staged bytes, not a re-download


def test_the_media_chain_validates_against_the_registry():
    registry.validate_action_chain([
        {"type": "drive_find_file", "connection_id": "google",
         "name": "{title}"},
        {"type": "drive_read_file", "connection_id": "google",
         "file_id": "{steps.find.output.file.id}", "id": "read"},
        {"type": "slack_upload_file", "credential_id": "slack",
         "channel": "#alerts", "filename": "{steps.read.output.filename}",
         "source_s3": {"bucket": "{steps.read.output.bucket}",
                       "key": "{steps.read.output.key}"},
         "id": "send"},
    ])

    specs = registry.action_specs()
    assert specs["drive_read_file"] == \
        ({"connection_id", "file_id"}, {"export_as"})
    assert specs["slack_upload_file"] == \
        ({"channel", "filename"},
         {"connection_id", "credential_id", "source_url", "source_s3",
          "content", "title", "initial_comment", "thread_ts", "content_type"})

    with pytest.raises(registry.ActionError, match="filename"):
        registry.validate_action_chain(
            [{"type": "slack_upload_file", "channel": "#alerts"}])
    with pytest.raises(registry.ActionError, match="unknown keys"):
        registry.validate_action_chain(
            [{"type": "drive_read_file", "connection_id": "g", "file_id": "f",
              "bogus": 1}])


# --- zoom_find_meeting: find-or-create on the topic path ------------------------


def run_zoom(action, transport):
    with mock.patch("plugins.zoom.runners._zoom_connection",
                    return_value=dict(ZOOM_CONNECTION)), \
         mock.patch.object(tokens, "get_access_token",
                           return_value=("tok", {})):
        return run_zoom_find_meeting(
            {"type": "zoom_find_meeting", "connection_id": "zoom-main",
             **action}, EVENT, transport=transport)


def test_a_missed_topic_find_creates_the_meeting_when_asked():
    transport = OrderedTransport(
        (200, {"meetings": []}),
        (201, {"id": 9100, "topic": "Kubernetes invoice-4137",
               "start_time": "2026-10-01T09:00:00Z", "duration": 60,
               "join_url": "https://zoom.us/j/9100",
               "start_url": "https://zoom.us/s/9100", "password": "secret1"}))

    output = run_zoom({"topic": "Kubernetes {title}",
                       "create_if_missing": "true",
                       "start_time": "2026-10-01T09:00:00Z"}, transport)

    assert output == {"found": False, "created": True, "scheduled": True,
                      "meeting": {"id": "9100",
                                  "topic": "Kubernetes invoice-4137",
                                  "start_time": "2026-10-01T09:00:00Z",
                                  "duration": 60,
                                  "join_url": "https://zoom.us/j/9100",
                                  "start_url": "https://zoom.us/s/9100",
                                  "passcode": "secret1"}}
    find_call, create_call = transport.calls
    assert find_call["method"] == "GET"
    assert create_call["method"] == "POST"
    assert create_call["url"] == "https://api.zoom.us/v2/users/me/meetings"
    # The find's topic names the created meeting; zoom_create_meeting's rules
    # apply (scheduled start, duration defaulting to 60 minutes).
    assert json.loads(create_call["body"]) == {
        "topic": "Kubernetes invoice-4137", "type": 2,
        "start_time": "2026-10-01T09:00:00Z", "duration": 60}


def test_the_created_meeting_is_instant_without_a_start_time():
    transport = OrderedTransport(
        (200, {"meetings": []}),
        (201, {"id": 9101, "topic": "invoice-4137"}))

    output = run_zoom({"topic": "{title}", "create_if_missing": True}, transport)

    assert output == {"found": False, "created": True, "scheduled": False,
                      "meeting": {"id": "9101", "topic": "invoice-4137",
                                  "start_time": None, "duration": None,
                                  "join_url": None, "start_url": None,
                                  "passcode": None}}
    assert json.loads(transport.calls[1]["body"]) == \
        {"topic": "invoice-4137", "type": 1}


def test_a_miss_without_create_if_missing_stays_a_verdict():
    transport = OrderedTransport((200, {"meetings": []}))

    output = run_zoom({"topic": "nothing matches this"}, transport)

    assert output == {"found": False, "meeting": None, "matched_by": "topic",
                      "scope": "upcoming"}
    assert len(transport.calls) == 1  # the listing only; no create


def test_a_found_meeting_never_creates():
    transport = OrderedTransport(
        (200, {"meetings": [{"id": 9003, "topic": "Kubernetes Course Live"}]}))

    output = run_zoom({"topic": "kubernetes", "create_if_missing": "true"},
                      transport)

    assert output["found"] is True
    assert output["meeting"]["id"] == "9003"
    assert len(transport.calls) == 1


def test_the_meeting_id_path_never_creates():
    transport = OrderedTransport(
        (404, {"code": 3001, "message": "Meeting not found"}))

    output = run_zoom({"meeting_id": "9001", "topic": "Standup",
                       "create_if_missing": "true"}, transport)

    assert output == {"found": False, "meeting": None}
    assert len(transport.calls) == 1


def test_a_failed_find_or_create_create_raises():
    transport = OrderedTransport(
        (200, {"meetings": []}),
        (400, {"code": 300, "message": "Zoom is sad"}))

    with pytest.raises(RuntimeError, match="Zoom is sad"):
        run_zoom({"topic": "{title}", "create_if_missing": "true",
                  "start_time": "2026-10-01T09:00:00Z"}, transport)


def test_find_or_create_dispatches_and_validates_through_the_registry():
    transport = OrderedTransport(
        (200, {"meetings": []}),
        (201, {"id": 9102, "topic": "Standup"}))

    with mock.patch("plugins.zoom.runners._zoom_connection",
                    return_value=dict(ZOOM_CONNECTION)), \
         mock.patch.object(tokens, "get_access_token",
                           return_value=("tok", {})), \
         mock.patch.object(base, "_default_transport", transport):
        output = registry.ACTIONS["zoom_find_meeting"].run(
            {"type": "zoom_find_meeting", "connection_id": "zoom-main",
             "topic": "Standup", "create_if_missing": "true"}, EVENT, "wf-1")

    assert output["created"] is True
    assert output["found"] is False

    registry.validate_action_chain([
        {"type": "zoom_find_meeting", "connection_id": "zoom",
         "topic": "standup", "create_if_missing": "true",
         "start_time": "2026-10-01T09:00:00Z", "duration": "60",
         "agenda": "week 1"},
    ])
    with pytest.raises(registry.ActionError, match="not a number"):
        registry.validate_action_chain([
            {"type": "zoom_find_meeting", "connection_id": "zoom",
             "topic": "standup", "duration": "sixty"}])
