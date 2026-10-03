"""Action-staple tests for the sheets/telegram round: sheets_delete_row,
sheets_create_spreadsheet, telegram_send_photo, telegram_send_document.

Unit tests drive each registered runner with a fake provider transport and
the connection/token seams patched (the test_action_breadth /
test_drive_upload pattern), asserting method, URL, request body and the
step-output shape. The registry half checks that the new types are
registered with their field specs and that validate_action_chain accepts a
valid chain and rejects missing required fields and unknown keys.
"""
import json
import unittest
from unittest.mock import patch

from src.dapier.connectors import registry
from src.dapier.engine.actions.sheets import (
    run_sheets_create_spreadsheet,
    run_sheets_delete_row,
)
from plugins.telegram.runners.telegram import (
    run_telegram_send_document,
    run_telegram_send_photo,
)

import src.dapier.connectors  # noqa: F401  (import = registration)


GOOGLE_CONNECTION = {"connection_id": "google", "provider": "google",
                     "status": "connected", "credential_id": "oauth#google"}
TELEGRAM_CONNECTION = {"connection_id": "tg-bot", "provider": "telegram",
                       "status": "connected", "credential_id": "oauth#tg-bot"}
BOT_TOKEN = "123456:AAH9qN4Q7Efh3example_token_value123"

EVENT = {"connector": "schedule", "event": "schedule.fired",
         "occurred_at": "2026-09-28T09:00:00+00:00",
         "data": {"subject": "Invoice 4137"}}


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


def ok(result):
    """A Telegram Bot API success envelope around ``result``."""
    return json_body({"ok": True, "result": result})


def multipart_parts(call):
    """A form-data body split into ({name: value}, (filename, type, bytes))."""
    boundary = call["headers"]["content-type"].split("boundary=")[1].encode()
    fields = {}
    media = None
    for part in call["body"].split(b"--" + boundary)[1:-1]:
        part = part.lstrip(b"\r\n")
        if part.endswith(b"\r\n"):
            part = part[:-2]
        head, payload = part.split(b"\r\n\r\n", 1)
        name = head.split(b'name="', 1)[1].split(b'"', 1)[0].decode()
        if b'filename="' in head:
            filename = head.split(b'filename="', 1)[1].split(b'"', 1)[0].decode()
            content_type = head.split(b"Content-Type: ", 1)[1].decode()
            media = (filename, content_type, payload)
        else:
            fields[name] = payload.decode()
    return fields, media


# --- sheets_delete_row ----------------------------------------------------------


def run_delete(transport, action, event=None, steps=None):
    action = {"type": "sheets_delete_row", "connection_id": "google", **action}
    with patch("src.dapier.engine.actions.sheets._sheets_connection",
               return_value=dict(GOOGLE_CONNECTION)), \
         patch("src.dapier.connections.tokens.get_access_token",
               return_value=("tok", {})):
        return run_sheets_delete_row(action, event or EVENT, steps=steps,
                                     transport=transport)


SHEET_METADATA = {"sheets": [{"properties": {"sheetId": 42, "title": "todo"}}]}


class SheetsDeleteRowTests(unittest.TestCase):
    def test_resolves_the_sheet_id_then_deletes_the_dimension(self):
        transport = FakeTransport(
            ("fields=sheets(properties", 200, json_body(SHEET_METADATA)),
            (":batchUpdate", 200, json_body({})))

        output = run_delete(transport, {"spreadsheet_id": "ss-1",
                                        "worksheet": "todo", "row": "3"})

        self.assertEqual(output, {"row": 3, "deleted": True})
        self.assertEqual([call["method"] for call in transport.calls],
                         ["GET", "POST"])
        metadata_call, delete_call = transport.calls
        self.assertEqual(
            metadata_call["url"],
            "https://sheets.googleapis.com/v4/spreadsheets/ss-1"
            "?fields=sheets(properties(sheetId,title))")
        self.assertEqual(delete_call["headers"]["authorization"], "Bearer tok")
        self.assertEqual(delete_call["headers"]["content-type"], "application/json")
        self.assertEqual(json.loads(delete_call["body"]), {"requests": [
            {"deleteDimension": {"range": {"sheetId": 42, "dimension": "ROWS",
                                           "startIndex": 2, "endIndex": 3}}}]})

    def test_row_takes_a_lookup_step_template(self):
        transport = FakeTransport(
            ("fields=sheets(properties", 200, json_body(SHEET_METADATA)),
            (":batchUpdate", 200, json_body({})))

        output = run_delete(transport, {"spreadsheet_id": "ss-1",
                                        "worksheet": "todo",
                                        "row": "{steps.lookup.output.row}"},
                            steps={"lookup": {"output": {"row": 5}}})

        self.assertEqual(output, {"row": 5, "deleted": True})
        delete_call = transport.calls[1]
        self.assertEqual(json.loads(delete_call["body"])["requests"][0]
                         ["deleteDimension"]["range"],
                         {"sheetId": 42, "dimension": "ROWS",
                          "startIndex": 4, "endIndex": 5})

    def test_unknown_worksheet_names_the_title(self):
        transport = FakeTransport(
            ("fields=sheets(properties", 200, json_body(SHEET_METADATA)),
            (":batchUpdate", 200, json_body({})))

        with self.assertRaises(ValueError) as caught:
            run_delete(transport, {"spreadsheet_id": "ss-1",
                                   "worksheet": "archive", "row": "2"})
        self.assertIn("archive", str(caught.exception))
        self.assertEqual(len(transport.calls), 1)  # no batchUpdate followed

    def test_requires_a_whole_number_row(self):
        transport = FakeTransport()

        for row in ("soon", "", "0"):
            with self.assertRaises(ValueError):
                run_delete(transport, {"spreadsheet_id": "ss-1",
                                       "worksheet": "todo", "row": row})
        self.assertEqual(transport.calls, [])

    def test_metadata_error_stops_before_the_delete(self):
        transport = FakeTransport(
            ("fields=sheets(properties", 403,
             json_body({"error": {"message": "no sheets scope"}})),
            (":batchUpdate", 200, json_body({})))

        with self.assertRaises(RuntimeError) as caught:
            run_delete(transport, {"spreadsheet_id": "ss-1",
                                   "worksheet": "todo", "row": "2"})
        self.assertIn("HTTP 403", str(caught.exception))
        self.assertIn("no sheets scope", str(caught.exception))
        self.assertEqual(len(transport.calls), 1)


# --- sheets_create_spreadsheet --------------------------------------------------


def run_create(transport, action, event=None, steps=None):
    action = {"type": "sheets_create_spreadsheet", "connection_id": "google",
              **action}
    with patch("src.dapier.engine.actions.sheets._sheets_connection",
               return_value=dict(GOOGLE_CONNECTION)), \
         patch("src.dapier.connections.tokens.get_access_token",
               return_value=("tok", {})):
        return run_sheets_create_spreadsheet(action, event or EVENT, steps=steps,
                                             transport=transport)


CREATED = {"spreadsheetId": "ss-new-1",
           "spreadsheetUrl": "https://docs.google.com/spreadsheets/d/ss-new-1",
           "sheets": [{"properties": {"sheetId": 0, "title": "Sheet1"}}]}


class SheetsCreateSpreadsheetTests(unittest.TestCase):
    def test_posts_the_title_and_returns_the_new_spreadsheet(self):
        transport = FakeTransport(("v4/spreadsheets", 200, json_body(CREATED)))

        output = run_create(transport, {"title": "Weekly {subject}"})

        self.assertEqual(output, {
            "spreadsheet_id": "ss-new-1",
            "url": "https://docs.google.com/spreadsheets/d/ss-new-1",
            "worksheet": "Sheet1",
        })
        self.assertEqual(len(transport.calls), 1)
        call = transport.calls[0]
        self.assertEqual(call["method"], "POST")
        self.assertEqual(call["url"], "https://sheets.googleapis.com/v4/spreadsheets")
        self.assertEqual(call["headers"]["authorization"], "Bearer tok")
        self.assertEqual(call["headers"]["content-type"], "application/json")
        self.assertEqual(json.loads(call["body"]),
                         {"properties": {"title": "Weekly Invoice 4137"}})

    def test_headers_land_on_the_first_worksheet_at_a1(self):
        transport = FakeTransport(("v4/spreadsheets", 200, json_body(CREATED)))

        output = run_create(transport, {"title": "Weekly",
                                        "headers": ["Date", "Task"]})

        self.assertTrue(output["headers_applied"])
        self.assertEqual([call["method"] for call in transport.calls],
                         ["POST", "PUT"])
        headers_call = transport.calls[1]
        self.assertEqual(
            headers_call["url"],
            "https://sheets.googleapis.com/v4/spreadsheets/ss-new-1/values/"
            "Sheet1%21A1?valueInputOption=USER_ENTERED")
        self.assertEqual(json.loads(headers_call["body"]),
                         {"values": [["Date", "Task"]]})

    def test_header_cells_take_templates(self):
        transport = FakeTransport(("v4/spreadsheets", 200, json_body(CREATED)))

        output = run_create(transport, {"title": "Weekly",
                                        "headers": ["{subject}", ""]},
                            steps={})

        self.assertTrue(output["headers_applied"])
        self.assertEqual(json.loads(transport.calls[1]["body"]),
                         {"values": [["Invoice 4137", ""]]})

    def test_multi_row_headers_are_rejected(self):
        transport = FakeTransport(("v4/spreadsheets", 200, json_body(CREATED)))

        with self.assertRaises(ValueError) as caught:
            run_create(transport, {"title": "Weekly",
                                   "headers": [["a"], ["b"]]})
        self.assertIn("single row", str(caught.exception))

    def test_requires_a_title(self):
        transport = FakeTransport()

        with self.assertRaises(ValueError) as caught:
            run_create(transport, {"title": "  "})
        self.assertIn("requires a title", str(caught.exception))
        self.assertEqual(transport.calls, [])

    def test_create_error_surfaces_status_and_detail(self):
        transport = FakeTransport(
            ("v4/spreadsheets", 400,
             json_body({"error": {"message": "The caller does not have permission"}})))

        with self.assertRaises(RuntimeError) as caught:
            run_create(transport, {"title": "Weekly"})
        self.assertIn("HTTP 400", str(caught.exception))
        self.assertIn("does not have permission", str(caught.exception))


# --- telegram_send_photo / telegram_send_document -------------------------------


def run_media(runner, transport, action, event=None, steps=None,
              staged_bytes=b"staged-bytes"):
    type_ = ("telegram_send_photo" if runner is run_telegram_send_photo
             else "telegram_send_document")
    action = {"type": type_, **action}
    with patch("src.dapier.engine.actions.base._connected_connection",
               return_value=dict(TELEGRAM_CONNECTION)), \
         patch("src.dapier.connections.credentials.get_credential",
               return_value={"token": BOT_TOKEN}), \
         patch("src.dapier.engine.actions.base._s3_body",
               return_value=staged_bytes) as s3_body:
        output = runner(action, event or EVENT, steps=steps, transport=transport)
    return output, s3_body


class TelegramSendPhotoTests(unittest.TestCase):
    def test_downloads_the_url_then_uploads_multipart(self):
        transport = FakeTransport(
            ("example.test/report", 200, b"\x89PNG fake photo bytes"),
            ("sendPhoto", 200, ok({"message_id": 9, "chat": {"id": 555}})))

        output, _s3 = run_media(
            run_telegram_send_photo, transport,
            {"connection_id": "tg-bot", "chat_id": "555",
             "source_url": "https://example.test/report.png"})

        self.assertEqual(output, {"message_id": 9, "chat_id": 555})
        self.assertEqual([call["method"] for call in transport.calls],
                         ["GET", "POST"])
        send_call = transport.calls[1]
        self.assertEqual(send_call["url"],
                         f"https://api.telegram.org/bot{BOT_TOKEN}/sendPhoto")
        self.assertTrue(send_call["headers"]["content-type"]
                        .startswith("multipart/form-data; boundary="))
        fields, media = multipart_parts(send_call)
        self.assertEqual(fields, {"chat_id": "555"})
        self.assertEqual(media, ("report.png", "image/png",
                                 b"\x89PNG fake photo bytes"))

    def test_chat_id_falls_back_to_the_triggering_chat_and_caption_renders(self):
        transport = FakeTransport(
            ("example.test/pic", 200, b"gif-bytes"),
            ("sendPhoto", 200, ok({"message_id": 10, "chat": {"id": 777}})))

        output, _s3 = run_media(
            run_telegram_send_photo, transport,
            {"connection_id": "tg-bot",
             "source_url": "https://example.test/pic.gif",
             "caption": "New mail: {subject}"},
            event={"connector": "telegram", "event": "message.received",
                   "data": {"chat_id": 777, "subject": "Invoice 4137"}})

        self.assertEqual(output, {"message_id": 10, "chat_id": 777})
        fields, media = multipart_parts(transport.calls[1])
        self.assertEqual(fields, {"chat_id": "777",
                                  "caption": "New mail: Invoice 4137"})
        self.assertEqual(media[0], "pic.gif")
        self.assertEqual(media[1], "image/gif")

    def test_no_caption_omits_the_field(self):
        transport = FakeTransport(
            ("example.test/pic", 200, b"bytes"),
            ("sendPhoto", 200, ok({"message_id": 11})))

        run_media(run_telegram_send_photo, transport,
                  {"connection_id": "tg-bot", "chat_id": 555,
                   "source_url": "https://example.test/pic.webp"})

        fields, _media = multipart_parts(transport.calls[1])
        self.assertEqual(fields, {"chat_id": "555"})

    def test_filename_override_wins(self):
        transport = FakeTransport(
            ("example.test/pic", 200, b"bytes"),
            ("sendPhoto", 200, ok({"message_id": 12})))

        run_media(run_telegram_send_photo, transport,
                  {"connection_id": "tg-bot", "chat_id": 555,
                   "source_url": "https://example.test/pic.bin",
                   "filename": "receipt.png"})

        _fields, media = multipart_parts(transport.calls[1])
        self.assertEqual(media[:2], ("receipt.png", "image/png"))

    def test_telegram_rejection_names_the_description(self):
        transport = FakeTransport(
            ("example.test/pic", 200, b"bytes"),
            ("sendPhoto", 200, json_body(
                {"ok": False, "description": "PHOTO_INVALID_DIMENSIONS"})))

        with self.assertRaises(telegram_api_error()) as caught:
            run_media(run_telegram_send_photo, transport,
                      {"connection_id": "tg-bot", "chat_id": 555,
                       "source_url": "https://example.test/pic.png"})
        self.assertIn("PHOTO_INVALID_DIMENSIONS", str(caught.exception))


class TelegramSendDocumentTests(unittest.TestCase):
    def test_sends_the_staged_object_without_a_download(self):
        transport = FakeTransport(
            ("sendDocument", 200, ok({"message_id": 13, "chat": {"id": 555}})))

        output, s3_body = run_media(
            run_telegram_send_document, transport,
            {"connection_id": "tg-bot", "chat_id": "555",
             "source_s3": {"bucket": "staging",
                           "key": "invoices/report.pdf"},
             "caption": "Invoice {subject}"},
            staged_bytes=b"%PDF-1.4 staged")

        self.assertEqual(output, {"message_id": 13, "chat_id": 555})
        s3_body.assert_called_once_with(
            {"bucket": "staging", "key": "invoices/report.pdf"})
        self.assertEqual([call["method"] for call in transport.calls], ["POST"])
        send_call = transport.calls[0]
        self.assertEqual(send_call["url"],
                         f"https://api.telegram.org/bot{BOT_TOKEN}/sendDocument")
        fields, media = multipart_parts(send_call)
        self.assertEqual(fields, {"chat_id": "555", "caption": "Invoice Invoice 4137"})
        self.assertEqual(media, ("report.pdf", "application/pdf", b"%PDF-1.4 staged"))

    def test_source_url_and_source_s3_takes_templates(self):
        transport = FakeTransport(
            ("sendDocument", 200, ok({"message_id": 14})))

        output, s3_body = run_media(
            run_telegram_send_document, transport,
            {"connection_id": "tg-bot", "chat_id": "555",
             "source_s3": {"bucket": "{steps.read.output.bucket}",
                           "key": "{steps.read.output.key}"},
             "filename": "{subject}.pdf"},
            event={"data": {"subject": "invoice"}},
            steps={"read": {"output": {"bucket": "staging",
                                       "key": "x/invoice.pdf"}}},
            staged_bytes=b"staged")

        self.assertEqual(output["message_id"], 14)
        s3_body.assert_called_once_with(
            {"bucket": "staging", "key": "x/invoice.pdf"})
        _fields, media = multipart_parts(transport.calls[0])
        self.assertEqual(media[:2], ("invoice.pdf", "application/pdf"))

    def test_one_source_only(self):
        transport = FakeTransport()

        with self.assertRaises(ValueError) as caught:
            run_media(run_telegram_send_document, transport,
                      {"connection_id": "tg-bot", "chat_id": "555",
                       "source_url": "https://example.test/f.png",
                       "source_s3": {"bucket": "b", "key": "k"}})
        self.assertIn("one media source", str(caught.exception))
        with self.assertRaises(ValueError) as caught:
            run_media(run_telegram_send_document, transport,
                      {"connection_id": "tg-bot", "chat_id": "555"})
        self.assertIn("needs source_url", str(caught.exception))
        self.assertEqual(transport.calls, [])

    def test_missing_chat_fails_before_any_call(self):
        transport = FakeTransport()

        with self.assertRaises(ValueError) as caught:
            run_media(run_telegram_send_photo, transport,
                      {"connection_id": "tg-bot",
                       "source_url": "https://example.test/f.png"},
                      event={"data": {}})
        self.assertIn("chat_id", str(caught.exception))
        self.assertEqual(transport.calls, [])

    def test_http_error_carries_telegrams_description(self):
        transport = FakeTransport(
            ("example.test/f.png", 200, b"bytes"),
            ("sendPhoto", 400, json_body(
                {"ok": False, "description": "chat not found"})))

        with self.assertRaises(telegram_api_error()) as caught:
            run_media(run_telegram_send_photo, transport,
                      {"connection_id": "tg-bot", "chat_id": "404",
                       "source_url": "https://example.test/f.png"})
        self.assertIn("chat not found", str(caught.exception))


def telegram_api_error():
    from src.dapier.connections.providers import telegram_api

    return telegram_api.TelegramApiError


# --- registry wiring ------------------------------------------------------------


class RegistryTests(unittest.TestCase):
    def test_new_actions_are_registered_with_their_specs(self):
        specs = registry.action_specs()
        self.assertEqual(specs["sheets_delete_row"],
                         ({"connection_id", "spreadsheet_id", "worksheet", "row"},
                          frozenset()))
        self.assertEqual(specs["sheets_create_spreadsheet"],
                         ({"connection_id", "title"}, {"headers"}))
        media_optional = {"chat_id", "source_url", "source_s3", "filename",
                          "caption", "timeout_seconds"}
        self.assertEqual(specs["telegram_send_photo"],
                         ({"connection_id"}, media_optional))
        self.assertEqual(specs["telegram_send_document"],
                         ({"connection_id"}, media_optional))
        row_field = next(field for field
                         in registry.ACTIONS["sheets_delete_row"].fields
                         if field["key"] == "row")
        self.assertEqual(row_field["type"], "number")
        photo_field = next(field for field
                           in registry.ACTIONS["telegram_send_photo"].fields
                           if field["key"] == "chat_id")
        self.assertEqual(photo_field.get("discover"),
                         {"resource": "telegram.chats"})

    def test_a_chain_using_the_new_actions_validates(self):
        registry.validate_action_chain([
            {"type": "sheets_lookup_row", "connection_id": "google",
             "spreadsheet_id": "ss-1", "worksheet": "todo",
             "column": "B", "value": "{text}"},
            {"type": "sheets_delete_row", "connection_id": "google",
             "spreadsheet_id": "ss-1", "worksheet": "todo",
             "row": "{steps.lookup.output.row}"},
            {"type": "sheets_create_spreadsheet", "connection_id": "google",
             "title": "Weekly {subject}", "headers": ["Date", "Task"]},
            {"type": "sheets_append_row", "connection_id": "google",
             "spreadsheet_id": "{steps.create.output.spreadsheet_id}",
             "values": ["{subject}"]},
            {"type": "telegram_send_document", "connection_id": "tg-bot",
             "source_s3": {"bucket": "{steps.read.output.bucket}",
                           "key": "{steps.read.output.key}"}},
            {"type": "telegram_send_photo", "connection_id": "tg-bot",
             "chat_id": "{steps.find.output.chat}",
             "source_url": "{steps.link.output.link}"},
        ])

    def test_validation_rejects_missing_and_unknown_keys(self):
        with self.assertRaises(registry.ActionError) as caught:
            registry.validate_action_chain([
                {"type": "sheets_delete_row", "connection_id": "google",
                 "spreadsheet_id": "ss-1", "worksheet": "todo"}])
        self.assertIn("missing: row", str(caught.exception))
        with self.assertRaises(registry.ActionError) as caught:
            registry.validate_action_chain([
                {"type": "sheets_create_spreadsheet", "connection_id": "google",
                 "title": "Weekly", "sheet_name": "Sheet1"}])
        self.assertIn("unknown keys: sheet_name", str(caught.exception))
        with self.assertRaises(registry.ActionError) as caught:
            registry.validate_action_chain([
                {"type": "telegram_send_photo", "connection_id": "tg-bot",
                 "source_url": "https://e.test/f.png", "filter": "x"}])
        self.assertIn("unknown keys: filter", str(caught.exception))

    def test_typed_source_url_rejects_a_non_url_literal(self):
        with self.assertRaises(registry.ActionError) as caught:
            registry.validate_action_chain([
                {"type": "telegram_send_photo", "connection_id": "tg-bot",
                 "source_url": "not a url"}])
        self.assertIn("source_url", str(caught.exception))
        # a template stays legal — what it renders to is data, not config
        registry.validate_action_chain([
            {"type": "telegram_send_photo", "connection_id": "tg-bot",
             "source_url": "{steps.link.output.link}"}])

    def test_engine_dispatch_runs_the_new_actions(self):
        transport = FakeTransport(
            ("fields=sheets(properties", 200, json_body(SHEET_METADATA)),
            (":batchUpdate", 200, json_body({})),
            ("v4/spreadsheets", 200, json_body(CREATED)),
            ("sendPhoto", 200, ok({"message_id": 15, "chat": {"id": 555}})),
            ("example.test/pic", 200, b"bytes"))
        with patch("src.dapier.engine.actions.base._connected_connection",
                   return_value=dict(TELEGRAM_CONNECTION)), \
             patch("src.dapier.engine.actions.sheets._sheets_connection",
                   return_value=dict(GOOGLE_CONNECTION)), \
             patch("src.dapier.connections.tokens.get_access_token",
                   return_value=("tok", {})), \
             patch("src.dapier.connections.credentials.get_credential",
                   return_value={"token": BOT_TOKEN}), \
             patch("src.dapier.engine.actions.base._default_transport", transport):
            deleted = registry.run_action(
                {"type": "sheets_delete_row", "connection_id": "google",
                 "spreadsheet_id": "ss-1", "worksheet": "todo", "row": "2"},
                EVENT, "wf-1")
            created = registry.run_action(
                {"type": "sheets_create_spreadsheet", "connection_id": "google",
                 "title": "Weekly"}, EVENT, "wf-1")
            photo = registry.run_action(
                {"type": "telegram_send_photo", "connection_id": "tg-bot",
                 "chat_id": "555", "source_url": "https://example.test/pic.png"},
                EVENT, "wf-1")

        self.assertEqual(deleted, {"row": 2, "deleted": True})
        self.assertEqual(created["spreadsheet_id"], "ss-new-1")
        self.assertEqual(photo["message_id"], 15)


if __name__ == "__main__":
    unittest.main()
