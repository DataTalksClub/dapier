"""dropbox_get_temp_link: a chain's read path to a file's bytes — a
short-lived direct-download link (files/get_temporary_link) that downstream
steps can pull, the same way Google Drive pipelines ride s3_upload's
source_url.

Unit tests drive the registered run callable with a fake transport (the
test_find_slack_dropbox FakeTransport pattern); the connection/token seams
are patched the same way, and the registry spec is checked.
"""
import json
import unittest
from unittest.mock import patch

from src.dapier.connectors import dropbox as dropbox_connector  # noqa: F401 (registers)
from src.dapier.connectors import registry
from src.dapier.engine.actions.dropbox import run_dropbox_get_temp_link


class FakeTransport:
    """Serves canned JSON payloads in call order and records every request."""

    def __init__(self, *payloads):
        self.payloads = list(payloads)
        self.calls = []

    def __call__(self, method, url, *, headers=None, body=None, timeout=15):
        self.calls.append({"method": method, "url": url,
                           "headers": headers, "body": body})
        if len(self.calls) > len(self.payloads):
            raise AssertionError(f"unexpected extra call: {method} {url}")
        return 200, json.dumps(self.payloads[len(self.calls) - 1]).encode()


DROPBOX_CONNECTION = {"connection_id": "dbx", "provider": "dropbox",
                      "status": "connected", "credential_id": "oauth#dbx",
                      "root_path": "/team/shared"}

TEMP_LINK = ("https://uc6f8.dl.dropboxusercontent.com/zip_download_get"
             "/report.pdf?dl=1")

LINK_RESPONSE = {
    "link": TEMP_LINK,
    "metadata": {"id": "id:f1", "name": "report.pdf", ".tag": "file",
                 "path_display": "/team/shared/report.pdf",
                 "size": 1234, "server_modified": "2026-09-26T21:03:49Z"},
}


def run_link(transport, action, event=None):
    action = {"type": "dropbox_get_temp_link", "connection_id": "dbx", **action}
    with patch("src.dapier.engine.actions.dropbox._dropbox_connection",
               return_value=DROPBOX_CONNECTION), \
         patch("src.dapier.connections.tokens.get_access_token",
               return_value=("tok", {})):
        return run_dropbox_get_temp_link(action, event or {"data": {}},
                                         transport=transport)


class DropboxGetTempLinkTests(unittest.TestCase):
    def test_returns_the_link_with_the_file_facts(self):
        transport = FakeTransport(LINK_RESPONSE)
        output = run_link(transport, {"path": "/team/shared/report.pdf"})

        self.assertEqual(output, {"link": TEMP_LINK, "item": {
            "id": "id:f1", "name": "report.pdf",
            "path": "/team/shared/report.pdf", "tag": "file",
            "size": 1234, "modified": "2026-09-26T21:03:49Z"}})
        call = transport.calls[0]
        self.assertEqual(call["method"], "POST")
        self.assertEqual(call["url"],
                         "https://api.dropboxapi.com/2/files/get_temporary_link")
        self.assertEqual(call["headers"]["authorization"], "Bearer tok")
        self.assertEqual(json.loads(call["body"]),
                         {"path": "/team/shared/report.pdf"})

    def test_path_renders_from_the_event(self):
        transport = FakeTransport(LINK_RESPONSE)
        run_link(transport, {"path": "{path}"},
                 event={"data": {"path": "/team/shared/invoice-4137.pdf"}})

        self.assertEqual(
            json.loads(transport.calls[0]["body"]),
            {"path": "/team/shared/invoice-4137.pdf"})

    def test_without_a_path_the_event_file_path_is_used(self):
        transport = FakeTransport(LINK_RESPONSE)
        output = run_link(transport, {},
                          event={"data": {"path": "/team/shared/report.pdf"}})

        self.assertTrue(output["link"])
        self.assertEqual(
            json.loads(transport.calls[0]["body"]),
            {"path": "/team/shared/report.pdf"})

    def test_missing_path_is_rejected_without_a_call(self):
        transport = FakeTransport()
        with self.assertRaises(ValueError):
            run_link(transport, {})
        self.assertEqual(transport.calls, [])

    def test_http_error_status_raises(self):
        class StatusTransport(FakeTransport):
            def __call__(self, method, url, *, headers=None, body=None, timeout=15):
                self.calls.append({"method": method, "url": url,
                                   "headers": headers, "body": body})
                return 409, json.dumps(
                    {"error": {".tag": "path", "path": {".tag": "not_found"}}}).encode()

        transport = StatusTransport()
        with patch("src.dapier.engine.actions.dropbox._dropbox_connection",
                   return_value=DROPBOX_CONNECTION), \
             patch("src.dapier.connections.tokens.get_access_token",
                   return_value=("tok", {})):
            with self.assertRaises(RuntimeError) as caught:
                run_dropbox_get_temp_link(
                    {"type": "dropbox_get_temp_link", "connection_id": "dbx",
                     "path": "/team/shared/gone.pdf"},
                    {"data": {}}, transport=transport)
        self.assertIn("HTTP 409", str(caught.exception))
        self.assertEqual(len(transport.calls), 1)

    def test_unreachable_transport_maps_to_a_runtime_error(self):
        def broken(method, url, *, headers=None, body=None, timeout=15):
            raise ConnectionError("no route to host")

        with patch("src.dapier.engine.actions.dropbox._dropbox_connection",
                   return_value=DROPBOX_CONNECTION), \
             patch("src.dapier.connections.tokens.get_access_token",
                   return_value=("tok", {})):
            with self.assertRaises(RuntimeError) as caught:
                run_dropbox_get_temp_link(
                    {"type": "dropbox_get_temp_link", "connection_id": "dbx",
                     "path": "/team/shared/report.pdf"},
                    {"data": {}}, transport=broken)
        self.assertIn("dropbox temp-link unreachable", str(caught.exception))
        self.assertIn("ConnectionError", str(caught.exception))

    def test_unreadable_body_raises(self):
        def junk(method, url, *, headers=None, body=None, timeout=15):
            return 200, b"<html>not json</html>"

        with patch("src.dapier.engine.actions.dropbox._dropbox_connection",
                   return_value=DROPBOX_CONNECTION), \
             patch("src.dapier.connections.tokens.get_access_token",
                   return_value=("tok", {})):
            with self.assertRaises(RuntimeError) as caught:
                run_dropbox_get_temp_link(
                    {"type": "dropbox_get_temp_link", "connection_id": "dbx",
                     "path": "/team/shared/report.pdf"},
                    {"data": {}}, transport=junk)
        self.assertIn("unreadable body", str(caught.exception))

    def test_response_without_metadata_yields_a_null_item(self):
        transport = FakeTransport({"link": TEMP_LINK})
        output = run_link(transport, {"path": "/team/shared/report.pdf"})

        self.assertEqual(output, {"link": TEMP_LINK, "item": None})

    def test_a_linkless_response_fails_loudly(self):
        transport = FakeTransport({"metadata": {"id": "id:f1", "name": "report.pdf"}})
        with patch("src.dapier.engine.actions.dropbox._dropbox_connection",
                   return_value=DROPBOX_CONNECTION), \
             patch("src.dapier.connections.tokens.get_access_token",
                   return_value=("tok", {})):
            with self.assertRaises(RuntimeError) as caught:
                run_dropbox_get_temp_link(
                    {"type": "dropbox_get_temp_link", "connection_id": "dbx",
                     "path": "/team/shared/report.pdf"},
                    {"data": {}}, transport=transport)
        self.assertIn("returned no link", str(caught.exception))


class RegistrationTests(unittest.TestCase):
    def test_the_action_is_registered_with_its_field_spec(self):
        self.assertIn("dropbox_get_temp_link", registry.ACTIONS)
        self.assertEqual(registry.action_specs()["dropbox_get_temp_link"],
                         ({"connection_id"}, {"path"}))

    def test_a_chain_using_the_action_validates(self):
        registry.validate_action_chain([
            {"type": "dropbox_get_temp_link", "connection_id": "dropbox",
             "path": "{path}"},
        ])
        with self.assertRaises(registry.ActionError):
            registry.validate_action_chain([
                {"type": "dropbox_get_temp_link", "connection_id": "dropbox",
                 "folder": "/Invoices"}  # unknown key
            ])

    def test_engine_dispatch_runs_the_action_end_to_end(self):
        """registry.run_action drives the registered lambda with the default
        transport — patch the seams it resolves and dispatch end to end."""
        transport = FakeTransport(LINK_RESPONSE)
        with patch("src.dapier.engine.actions.base._connected_connection",
                   return_value=dict(DROPBOX_CONNECTION)), \
             patch("src.dapier.engine.actions.base._default_transport", transport), \
             patch("src.dapier.connections.tokens.get_access_token",
                   return_value=("tok", {})):
            output = registry.run_action(
                {"type": "dropbox_get_temp_link", "connection_id": "dbx",
                 "path": "/team/shared/report.pdf"},
                {"data": {}}, "wf-1")

        self.assertEqual(output["link"], TEMP_LINK)
        self.assertEqual(output["item"]["id"], "id:f1")
