"""The quick actions: drive_move_file, drive_delete_file's permanent flag,
sheets_add_worksheet, zoom_delete_recording, s3_list_objects, and email
threading (in_reply_to/references).

Unit tests drive each registered runner with fake transports / clients and
the connection/token seams patched (the test_action_breadth pattern),
asserting method, URL, request body and the step-output shape. The registry
half checks the new types are registered, validate_action_chain accepts a
valid chain and rejects missing required fields and unknown keys, and that
save-time field typing rejects non-numeric count literals.
"""
import json
import unittest
import unittest.mock as mock
import urllib.parse
from datetime import datetime, timezone
from email import policy
from email.parser import BytesParser

import pytest

from src.dapier.connectors import registry
from src.dapier.connections import tokens
from src.dapier.engine.actions.drive import (
    run_drive_delete_file,
    run_drive_move_file,
)
from src.dapier.engine.actions.email import run_email_send
from plugins.aws.runners.s3 import run_s3_list_objects
from src.dapier.engine.actions.sheets import run_sheets_add_worksheet
from plugins.zoom.runners import run_zoom_delete_recording

import src.dapier.connectors  # noqa: F401  (import = registration)


class FakeTransport:
    """Route provider calls by URL substring to canned JSON responses."""

    def __init__(self, *routes):
        self.routes = routes  # (substring, status, payload) tuples
        self.calls = []

    def __call__(self, method, url, *, headers=None, body=None, timeout=15):
        self.calls.append({"method": method, "url": url, "headers": headers,
                           "body": body})
        for substring, status, payload in self.routes:
            if substring in url:
                return status, json.dumps(payload).encode()
        raise AssertionError(f"unexpected provider call: {method} {url}")


GOOGLE_CONNECTION = {"connection_id": "google", "provider": "google",
                     "status": "connected"}
ZOOM_CONNECTION = {"connection_id": "zoom-main", "provider": "zoom",
                   "status": "connected", "credential_id": "oauth#zoom-main"}

EVENT = {"connector": "schedule", "event": "schedule.fired",
         "occurred_at": "2026-09-28T09:00:00+00:00",
         "data": {"file_id": "1a2B3c", "message_id": "<orig@x.test>",
                  "topic": "Live lecture", "month": "09"}}


@pytest.fixture(autouse=True)
def token_seam(monkeypatch):
    """Every connection's access token resolves to a stored secret."""
    monkeypatch.setattr(tokens, "get_access_token",
                        lambda connection, transport=None: ("tok", {}))


def _query(url):
    """The parsed query string of one drive/sheets/zoom call URL."""
    return urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)


# --- drive_move_file ----------------------------------------------------------


def run_drive_move(action, transport):
    with mock.patch("src.dapier.engine.actions.base._connected_connection",
                    return_value=GOOGLE_CONNECTION):
        return run_drive_move_file(
            {"type": "drive_move_file", "connection_id": "google", **action},
            EVENT, transport=transport)


def test_drive_move_file_sends_add_and_remove_parents():
    transport = FakeTransport(
        ("drive/v3/files", 200,
         {"id": "1a2B3c", "name": "invoice.pdf",
          "parents": ["folder-inbox"]}))
    with mock.patch("src.dapier.engine.actions.base._connected_connection",
                    return_value=GOOGLE_CONNECTION):
        output = run_drive_move_file(
            {"type": "drive_move_file", "connection_id": "google",
             "file_id": "{file_id}", "add_parent": "folder-inbox",
             "remove_parent": "folder-archive"},
            EVENT, transport=transport)

    assert output == {"moved": True, "file_id": "1a2B3c",
                      "name": "invoice.pdf", "parents": ["folder-inbox"]}
    call = transport.calls[0]
    assert call["method"] == "PATCH"
    assert call["url"].startswith("https://www.googleapis.com/drive/v3/files/1a2B3c?")
    query = _query(call["url"])
    assert query["addParents"] == ["folder-inbox"]
    assert query["removeParents"] == ["folder-archive"]
    assert query["supportsAllDrives"] == ["true"]
    assert query["fields"] == ["id,name,parents"]
    assert call["headers"]["authorization"] == "Bearer tok"


def test_drive_move_file_add_only_keeps_the_file_where_it_was_too():
    transport = FakeTransport(
        ("drive/v3/files", 200,
         {"id": "1a2B3c", "name": "invoice.pdf",
          "parents": ["folder-a", "folder-b"]}))
    run_drive_move({"file_id": "1a2B3c", "add_parent": "folder-b"}, transport)

    query = _query(transport.calls[0]["url"])
    assert "addParents" in query
    assert "removeParents" not in query


def test_drive_move_file_without_any_parent_fails():
    transport = FakeTransport()
    with pytest.raises(ValueError, match="add_parent or remove_parent"):
        run_drive_move({"file_id": "1a2B3c"}, transport)
    assert transport.calls == []


def test_drive_move_file_provider_error_raises():
    transport = FakeTransport(
        ("drive/v3/files", 404,
         {"error": {"message": "File not found: 1a2B3c."}}))
    with pytest.raises(RuntimeError) as excinfo:
        run_drive_move({"file_id": "1a2B3c", "add_parent": "folder-x"},
                       transport)
    assert "HTTP 404" in str(excinfo.value)
    assert "File not found" in str(excinfo.value)


# --- drive_delete_file: trash by default, files.delete when permanent --------


def run_drive_delete(action, transport):
    with mock.patch("src.dapier.engine.actions.base._connected_connection",
                    return_value=GOOGLE_CONNECTION):
        return run_drive_delete_file(
            {"type": "drive_delete_file", "connection_id": "google", **action},
            EVENT, transport=transport)


def test_drive_delete_file_trashes_by_default():
    transport = FakeTransport(
        ("drive/v3/files", 200,
         {"id": "1a2B3c", "name": "invoice.pdf", "trashed": True}))
    output = run_drive_delete({"file_id": "{file_id}"}, transport)

    assert output == {"trashed": True, "permanent": False,
                      "file_id": "1a2B3c", "name": "invoice.pdf"}
    call = transport.calls[0]
    assert call["method"] == "PATCH"
    assert json.loads(call["body"]) == {"trashed": True}
    assert _query(call["url"])["fields"] == ["id,name,trashed"]


def test_drive_delete_file_permanent_uses_files_delete():
    transport = FakeTransport(("drive/v3/files", 204, {}))
    output = run_drive_delete({"file_id": "1a2B3c", "permanent": True},
                              transport)

    assert output == {"trashed": False, "permanent": True,
                      "file_id": "1a2B3c", "name": None}
    call = transport.calls[0]
    assert call["method"] == "DELETE"
    assert call["url"] == "https://www.googleapis.com/drive/v3/files/1a2B3c?supportsAllDrives=true&fields=id%2Cname%2Ctrashed"
    assert call["body"] is None


def test_drive_delete_file_accepts_the_designer_boolean_string():
    transport = FakeTransport(("drive/v3/files", 204, {}))
    output = run_drive_delete({"file_id": "1a2B3c", "permanent": "true"},
                              transport)
    assert transport.calls[0]["method"] == "DELETE"
    assert output["permanent"] is True


def test_drive_delete_file_requires_a_file_id():
    transport = FakeTransport()
    with pytest.raises(ValueError, match="requires a file_id"):
        run_drive_delete({"file_id": "  "}, transport)
    assert transport.calls == []


def test_drive_delete_file_permanent_error_raises():
    transport = FakeTransport(
        ("drive/v3/files", 403,
         {"error": {"message": "Insufficient permissions"}}))
    with pytest.raises(RuntimeError) as excinfo:
        run_drive_delete({"file_id": "1a2B3c", "permanent": True}, transport)
    assert "HTTP 403" in str(excinfo.value)


# --- sheets_add_worksheet -----------------------------------------------------


def run_sheets_add(action, transport):
    with mock.patch("src.dapier.engine.actions.sheets._sheets_connection",
                    return_value=GOOGLE_CONNECTION):
        return run_sheets_add_worksheet(
            {"type": "sheets_add_worksheet", "connection_id": "google",
             "spreadsheet_id": "ss-1", **action},
            EVENT, transport=transport)


def test_sheets_add_worksheet_posts_an_addsheet_request():
    transport = FakeTransport(
        (":batchUpdate", 200,
         {"spreadsheetId": "ss-1",
          "replies": [{"addSheet": {"properties": {
              "sheetId": 42, "title": "Archive 09",
              "gridProperties": {"rowCount": 100, "columnCount": 5}}}}]}))
    output = run_sheets_add(
        {"title": "Archive {month}",
         "row_count": "100", "column_count": "5"}, transport)

    assert output == {"sheet_id": 42, "title": "Archive 09",
                      "row_count": 100, "column_count": 5,
                      "spreadsheet_id": "ss-1"}
    call = transport.calls[0]
    assert call["method"] == "POST"
    assert call["url"] == "https://sheets.googleapis.com/v4/spreadsheets/ss-1:batchUpdate"
    assert call["headers"]["authorization"] == "Bearer tok"
    assert json.loads(call["body"]) == {"requests": [{"addSheet": {
        "properties": {"title": "Archive 09",
                       "gridProperties": {"rowCount": 100, "columnCount": 5}}}}]}


def test_sheets_add_worksheet_defaults_to_sheets_own_grid_size():
    transport = FakeTransport(
        (":batchUpdate", 200,
         {"replies": [{"addSheet": {"properties": {
             "sheetId": 7, "title": "todo"}}}]}))
    output = run_sheets_add({"title": "todo"}, transport)

    assert json.loads(transport.calls[0]["body"]) == {"requests": [{
        "addSheet": {"properties": {
            "title": "todo",
            "gridProperties": {"rowCount": 1000, "columnCount": 26}}}}]}
    assert output["row_count"] == 1000
    assert output["column_count"] == 26


def test_sheets_add_worksheet_requires_a_title():
    with pytest.raises(ValueError, match="requires a title"):
        run_sheets_add({"title": "  "}, FakeTransport())


def test_sheets_add_worksheet_rejects_a_bad_row_count():
    with pytest.raises(ValueError, match="row_count"):
        run_sheets_add({"title": "todo", "row_count": "lots"}, FakeTransport())
    with pytest.raises(ValueError, match="row_count"):
        run_sheets_add({"title": "todo", "row_count": "0"}, FakeTransport())


def test_sheets_add_worksheet_duplicate_title_is_a_runtime_error():
    transport = FakeTransport(
        (":batchUpdate", 400,
         {"error": {"message": "Invalid requests[0].addSheet: A sheet with "
                               "the name \"todo\" already exists."}}))
    with pytest.raises(RuntimeError) as excinfo:
        run_sheets_add({"title": "todo"}, transport)
    assert "HTTP 400" in str(excinfo.value)
    assert "already exists" in str(excinfo.value)


# --- zoom_delete_recording ----------------------------------------------------


def run_zoom_delete_recording_with(action, transport):
    with mock.patch("plugins.zoom.runners._zoom_connection",
                    return_value=ZOOM_CONNECTION):
        return run_zoom_delete_recording(
            {"type": "zoom_delete_recording", "connection_id": "zoom-main",
             **action},
            EVENT, transport=transport)


def test_zoom_delete_recording_trashes_by_default():
    transport = FakeTransport(("/recordings", 204, {}))
    output = run_zoom_delete_recording_with(
        {"meeting_id": "94839610293"}, transport)

    assert output == {"deleted": True, "meeting_id": "94839610293",
                      "action": "trash"}
    call = transport.calls[0]
    assert call["method"] == "DELETE"
    assert call["url"].startswith(
        "https://api.zoom.us/v2/meetings/94839610293/recordings")
    assert _query(call["url"])["action"] == ["trash"]
    assert call["headers"]["authorization"] == "Bearer tok"


def test_zoom_delete_recording_permanent_omits_the_action_flag():
    transport = FakeTransport(("/recordings", 204, {}))
    output = run_zoom_delete_recording_with(
        {"meeting_id": "94839610293", "action": "permanent"}, transport)

    assert output["action"] == "permanent"
    assert "action" not in _query(transport.calls[0]["url"])


def test_zoom_delete_recording_rejects_an_unknown_action():
    with pytest.raises(ValueError, match="trash, permanent"):
        run_zoom_delete_recording_with(
            {"meeting_id": "94839610293", "action": " shred"}, FakeTransport())


def test_zoom_delete_recording_requires_a_meeting_id():
    with pytest.raises(ValueError, match="requires meeting_id"):
        run_zoom_delete_recording_with({"meeting_id": ""}, FakeTransport())


def test_zoom_delete_recording_provider_error_raises():
    transport = FakeTransport(
        ("/recordings", 404, {"message": "Recording does not exist"}))
    with pytest.raises(RuntimeError) as excinfo:
        run_zoom_delete_recording_with({"meeting_id": "1234"}, transport)
    assert "HTTP 404" in str(excinfo.value)
    assert "Recording does not exist" in str(excinfo.value)


# --- s3_list_objects ----------------------------------------------------------


def s3_page(contents, *, truncated=False, token=None):
    """One canned list_objects_v2 response."""
    response = {"Contents": contents}
    if truncated:
        response["IsTruncated"] = True
        response["NextContinuationToken"] = token
    return response


class FakeS3:
    """list_objects_v2 hands out the canned pages in call order."""

    def __init__(self, *pages):
        self.pages = list(pages)
        self.calls = []

    def list_objects_v2(self, **kwargs):
        self.calls.append(kwargs)
        if len(self.calls) > len(self.pages):
            raise AssertionError("unexpected extra list_objects_v2 page")
        return self.pages[len(self.calls) - 1]


def run_s3_list(overrides=None, *, s3_client):
    action = {"type": "s3_list_objects", "bucket": "backups", **(overrides or {})}
    return run_s3_list_objects(action, {"data": {}}, steps={},
                               s3_client=s3_client)


def test_s3_list_objects_lists_key_size_and_last_modified():
    s3 = FakeS3(s3_page([
        {"Key": "reports/a.pdf", "Size": 10,
         "LastModified": datetime(2026, 9, 26, 21, 3, 49,
                                  tzinfo=timezone.utc)},
        {"Key": "reports/b.pdf", "Size": 20, "LastModified": "2026-09-27"},
    ]))
    output = run_s3_list({"prefix": "reports/"}, s3_client=s3)

    assert output == {"bucket": "backups", "prefix": "reports/",
                      "count": 2, "truncated": False, "next_token": "",
                      "items": [
                          {"key": "reports/a.pdf", "size": 10,
                           "last_modified": "2026-09-26T21:03:49+00:00"},
                          {"key": "reports/b.pdf", "size": 20,
                           "last_modified": "2026-09-27"},
                      ]}
    assert s3.calls == [{"Bucket": "backups", "MaxKeys": 20,
                         "Prefix": "reports/"}]


def test_s3_list_objects_empty_prefix_lists_the_whole_bucket():
    s3 = FakeS3(s3_page([]))
    output = run_s3_list({}, s3_client=s3)
    assert output["items"] == []
    assert output["count"] == 0
    assert output["prefix"] is None
    assert s3.calls[0] == {"Bucket": "backups", "MaxKeys": 20}


def test_s3_list_objects_caps_max_items_and_chains_the_token():
    s3 = FakeS3(
        s3_page([{"Key": "a", "Size": 1}],
                truncated=True, token="tok-2"),
        s3_page([{"Key": "b", "Size": 2}]),
    )
    output = run_s3_list({"max_items": 2, "next_token": "tok-1"},
                         s3_client=s3)

    assert [call["ContinuationToken"] for call in s3.calls] == ["tok-1", "tok-2"]
    assert [call["MaxKeys"] for call in s3.calls] == [2, 1]
    assert [item["key"] for item in output["items"]] == ["a", "b"]
    assert output["count"] == 2
    assert output["truncated"] is False
    assert output["next_token"] == ""


def test_s3_list_objects_caps_at_100_and_reports_truncation():
    s3 = FakeS3(s3_page([{"Key": f"f{i}", "Size": i} for i in range(120)],
                        truncated=True, token="more"))
    output = run_s3_list({"max_items": 5000}, s3_client=s3)

    assert s3.calls[0]["MaxKeys"] == 100
    assert output["count"] == 100
    assert output["truncated"] is True
    assert output["next_token"] == "more"


def test_s3_list_objects_rejects_a_bad_max_items():
    with pytest.raises(ValueError, match="max_items"):
        run_s3_list({"max_items": "many"}, s3_client=FakeS3())
    with pytest.raises(ValueError, match="max_items"):
        run_s3_list({"max_items": "0"}, s3_client=FakeS3())


def test_s3_list_objects_requires_a_bucket():
    with pytest.raises(ValueError, match="requires a bucket"):
        run_s3_list({"bucket": "  "}, s3_client=FakeS3())


# --- email threading: in_reply_to / references --------------------------------


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


def parse_raw(ses):
    return BytesParser(policy=policy.default).parsebytes(
        ses.raw[0]["RawMessage"]["Data"])


def run_email(action, ses):
    return run_email_send(
        {"type": "email_send", "to": "ops@example.com", "sender": "bot@x.test",
         "text": "hi", **action},
        {"data": {"message_id": "<orig@x.test>"}}, ses=ses)


def test_plain_send_still_rides_the_simple_call():
    ses = StubSes()
    output = run_email({}, ses)
    assert ses.raw == []
    assert ses.sent[0]["Source"] == "bot@x.test"
    assert output["message_id"] == "mid-1"
    assert "in_reply_to" not in output


def test_in_reply_to_and_references_thread_the_text_send():
    ses = StubSes()
    output = run_email({"in_reply_to": "{message_id}",
                        "references": "{message_id}"}, ses)

    assert ses.sent == []
    message = parse_raw(ses)
    assert str(message["In-Reply-To"]) == "<orig@x.test>"
    assert str(message["References"]) == "<orig@x.test>"
    assert str(message["To"]) == "ops@example.com"
    assert output["message_id"] == "raw-1"
    assert output["in_reply_to"] == "<orig@x.test>"


def test_threading_headers_land_on_the_attachment_path_too():
    ses = StubSes()
    run_email({"in_reply_to": "<orig@x.test>",
               "attachments": [{"filename": "note.txt", "content": "plain"}]},
              ses)

    message = parse_raw(ses)
    assert str(message["In-Reply-To"]) == "<orig@x.test>"
    assert message.get_payload()[-1].get_content_type() == "text/plain"
    assert str(message["Subject"]) == "(no subject)"


def test_html_and_threading_headers_share_the_raw_path():
    ses = StubSes()
    run_email({"text": "", "html": "<p>hi</p>", "in_reply_to": "<orig@x.test>",
               "references": "<chain-1@x.test> <orig@x.test>"}, ses)

    message = parse_raw(ses)
    assert str(message["References"]) == "<chain-1@x.test> <orig@x.test>"
    assert message.get_content_type() == "text/html"


def test_absent_threading_fields_leave_no_headers_behind():
    ses = StubSes()
    run_email({"attachments": [{"filename": "note.txt", "content": "x"}]},
              ses)
    message = parse_raw(ses)
    assert message["In-Reply-To"] is None
    assert message["References"] is None


# --- registry wiring ----------------------------------------------------------


def test_new_actions_are_registered():
    specs = registry.action_specs()
    assert specs["drive_move_file"] == (
        {"connection_id", "file_id"}, {"add_parent", "remove_parent"})
    assert specs["drive_delete_file"] == (
        {"connection_id", "file_id"}, {"permanent"})
    assert specs["sheets_add_worksheet"] == (
        {"connection_id", "spreadsheet_id", "title"},
        {"row_count", "column_count"})
    assert specs["zoom_delete_recording"] == (
        {"connection_id", "meeting_id"}, {"action"})
    assert specs["s3_list_objects"] == (
        {"bucket"},
        {"prefix", "max_items", "next_token", "credential_id", "connection_id"})
    assert {"in_reply_to", "references"} <= set(specs["email_send"][1])
    permanent_field = next(field for field
                           in registry.ACTIONS["drive_delete_file"].fields
                           if field["key"] == "permanent")
    assert permanent_field["type"] == "boolean"
    move_field = next(field for field
                      in registry.ACTIONS["drive_move_file"].fields
                      if field["key"] == "add_parent")
    assert move_field["discover"] == {"resource": "google-drive.folders"}


def test_validate_action_chain_accepts_the_new_actions():
    registry.validate_action_chain([
        {"type": "s3_list_objects", "bucket": "backups", "prefix": "in/",
         "max_items": 50},
        {"type": "sheets_add_worksheet", "connection_id": "google",
         "spreadsheet_id": "ss-1", "title": "Archive", "row_count": 100,
         "column_count": 5},
        {"type": "zoom_delete_recording", "connection_id": "zoom",
         "meeting_id": "{steps.find.output.meeting_id}", "action": "trash"},
        {"type": "drive_move_file", "connection_id": "google",
         "file_id": "1a2B3c", "add_parent": "folder-inbox"},
        {"type": "drive_delete_file", "connection_id": "google",
         "file_id": "1a2B3c", "permanent": True},
        {"type": "email_send", "to": "ops@example.com",
         "in_reply_to": "{message_id}", "references": "{message_id}"},
    ])


def test_validate_action_chain_rejects_missing_required_fields():
    with pytest.raises(registry.ActionError, match="missing: meeting_id"):
        registry.validate_action_chain([
            {"type": "zoom_delete_recording", "connection_id": "zoom"}])
    with pytest.raises(registry.ActionError, match="missing: bucket"):
        registry.validate_action_chain([
            {"type": "s3_list_objects"}])
    with pytest.raises(registry.ActionError, match="missing: title"):
        registry.validate_action_chain([
            {"type": "sheets_add_worksheet", "connection_id": "google",
             "spreadsheet_id": "ss-1"}])


def test_validate_action_chain_rejects_unknown_keys():
    with pytest.raises(registry.ActionError, match="unknown keys: shred"):
        registry.validate_action_chain([
            {"type": "zoom_delete_recording", "connection_id": "zoom",
             "meeting_id": "1", "shred": True}])
    with pytest.raises(registry.ActionError, match="unknown keys: folder"):
        registry.validate_action_chain([
            {"type": "drive_move_file", "connection_id": "google",
             "file_id": "1a2B3c", "folder": "folder-x"}])
    with pytest.raises(registry.ActionError, match="unknown keys: thread_id"):
        registry.validate_action_chain([
            {"type": "email_send", "to": "ops@example.com",
             "thread_id": "1"}])


def test_count_fields_reject_non_numeric_literals():
    with pytest.raises(registry.ActionError, match="max_items"):
        registry.validate_action_chain([
            {"type": "s3_list_objects", "bucket": "b", "max_items": "many"}])
    with pytest.raises(registry.ActionError, match="row_count"):
        registry.validate_action_chain([
            {"type": "sheets_add_worksheet", "connection_id": "google",
             "spreadsheet_id": "ss-1", "title": "t", "row_count": "soon"}])
    # numeric and templated values stay legal
    registry.validate_field_types(
        {"type": "s3_list_objects", "max_items": 50}, "s3_list_objects")
    registry.validate_field_types(
        {"type": "sheets_add_worksheet",
         "row_count": "{steps.lookup.output.rows}"}, "sheets_add_worksheet")


def test_registry_dispatches_drive_move_and_zoom_delete():
    transport = FakeTransport(
        ("drive/v3/files", 200,
         {"id": "1a2B3c", "name": "invoice.pdf", "parents": ["folder-b"]}),
        ("/recordings", 204, {}))
    with mock.patch("src.dapier.engine.actions.base._connected_connection",
                    return_value=GOOGLE_CONNECTION), \
         mock.patch("src.dapier.engine.actions.base._default_transport",
                    transport):
        moved = registry.ACTIONS["drive_move_file"].run(
            {"type": "drive_move_file", "connection_id": "google",
             "file_id": "1a2B3c", "remove_parent": "folder-a",
             "add_parent": "folder-b"}, EVENT, "wf-1")
    with mock.patch("plugins.zoom.runners._zoom_connection",
                    return_value=ZOOM_CONNECTION), \
         mock.patch("src.dapier.engine.actions.base._default_transport",
                    transport):
        deleted = registry.ACTIONS["zoom_delete_recording"].run(
            {"type": "zoom_delete_recording", "connection_id": "zoom-main",
             "meeting_id": "94839610293"}, EVENT, "wf-1")

    assert moved["moved"] is True
    assert moved["parents"] == ["folder-b"]
    assert deleted == {"deleted": True, "meeting_id": "94839610293",
                       "action": "trash"}
    assert [call["method"] for call in transport.calls] == ["PATCH", "DELETE"]


if __name__ == "__main__":
    import sys
    sys.exit(pytest.main([__file__]))
