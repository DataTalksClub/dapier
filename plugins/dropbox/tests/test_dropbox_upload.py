"""dropbox_upload skip_existing: a path 409 is already-archived, not a failure."""
import json
import unittest
from copy import deepcopy
from unittest.mock import patch

import plugins.dropbox.plugin  # noqa: F401
from plugins.dropbox.runners.dropbox import run_dropbox_upload


class FakeTransport:
    def __init__(self, status=200, body=b'{"path_display": "/Invoices/x"}'):
        self.status = status
        self.body = body
        self.calls = []

    def __call__(self, method, url, *, headers, body, timeout=15):
        self.calls.append({"method": method, "url": url, "headers": headers, "body": body})
        return self.status, self.body


CONNECTION = {"connection_id": "dropbox", "provider": "dropbox", "status": "connected"}
EVENT = {
    "data": {
        "attachments": [{
            "filename": "invoice.pdf",
            "s3": {"bucket": "mail", "key": "msg/invoice.pdf"},
        }],
    },
}


def run(transport, action=None, event=None):
    action = action or {"type": "dropbox_upload", "connection_id": "dropbox",
                        "folder": "/Invoices"}
    with patch("plugins.dropbox.runners.dropbox._dropbox_connection",
               return_value=dict(CONNECTION)), \
         patch("src.dapier.connections.tokens.get_access_token",
               return_value=("token-123", {})), \
         patch("src.dapier.engine.actions.base._s3_body", return_value=b"pdf-bytes"):
        return run_dropbox_upload(action, event or deepcopy(EVENT), transport=transport)


class SkipExistingTests(unittest.TestCase):
    def test_path_conflict_returns_already_exists(self):
        transport = FakeTransport(
            status=409,
            body=b'{"error": {".tag": "path", "path": {".tag": "conflict"}}}')
        output = run(transport, {"type": "dropbox_upload", "connection_id": "dropbox",
                                 "folder": "/Invoices", "skip_existing": True})
        self.assertEqual(output, {
            "uploaded": ["/Invoices/invoice.pdf"], "already_exists": True})
        self.assertEqual(len(transport.calls), 1)

    def test_path_conflict_without_the_flag_still_raises(self):
        transport = FakeTransport(
            status=409,
            body=b'{"error": {".tag": "path", "path": {".tag": "conflict"}}}')
        with self.assertRaises(RuntimeError) as caught:
            run(transport)
        self.assertIn("HTTP 409", str(caught.exception))
        self.assertIn("path/conflict", str(caught.exception))

    def test_new_upload_is_not_already_there(self):
        output = run(FakeTransport())
        self.assertEqual(output["already_exists"], False)

    def test_registry_lists_skip_existing(self):
        from src.dapier.connectors import registry
        spec = registry.ACTIONS["dropbox_upload"]
        self.assertIn("skip_existing", spec.optional)
        keys = [field["key"] for field in spec.fields]
        self.assertIn("skip_existing", keys)
