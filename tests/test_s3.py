"""s3_upload: source fetch, key/content-type templating, and the S3 put."""
import unittest
from unittest.mock import patch

from src.dapier.connectors.registry import action_specs, run_action
from src.dapier.engine.actions.s3 import run_s3_upload


class FakeTransport:
    def __init__(self, status=200, body=b"file-bytes"):
        self.status = status
        self.body = body
        self.calls = []

    def __call__(self, method, url, *, headers, body, timeout=15):
        self.calls.append({"method": method, "url": url, "headers": headers,
                           "body": body, "timeout": timeout})
        return self.status, self.body


class FakeS3:
    def __init__(self):
        self.objects = {}

    def put_object(self, Bucket, Key, Body, **kwargs):
        self.objects[(Bucket, Key)] = (Body, kwargs)


DRIVE_FILE_EVENT = {
    "id": "drive-mailchimp-s3-1a2b3c4d5e6f7a8b", "connector": "poll",
    "event": "item.new", "source": "drive-mailchimp-s3",
    "occurred_at": "2026-09-26T21:03:50+00:00",
    "data": {"id": "1aBcD_drive_file_id", "name": "contacts export.csv",
             "mimeType": "text/csv", "createdTime": "2026-09-26T21:03:49.887Z",
             "size": "1234", "poll": "drive-mailchimp-s3",
             "item_id": "2026-09-26T21:03:49.887Z"},
}

ACTION = {
    "type": "s3_upload", "credential_id": "aws",
    "bucket": "datatalks-mailchimp-backup",
    "key": "mailchimp/{name}",
    "source_url": "https://www.googleapis.com/drive/v3/files/{id}?alt=media",
    "source_connection_id": "google-drive",
}


def run(overrides=None, event=None, *, transport=None, s3_client=None):
    transport = transport if transport is not None else FakeTransport()
    with patch("src.dapier.engine.actions.base._connected_connection",
               return_value={"connection_id": "google-drive", "provider": "google",
                             "status": "connected"}), \
         patch("src.dapier.connections.tokens.get_access_token",
               return_value=("drive-token", {})), \
         patch("src.dapier.connections.credentials.get_credential",
               return_value={"access_key_id": "AKIAEXAMPLE0000", "secret_access_key": "b" * 40}):
        return run_s3_upload({**ACTION, **(overrides or {})}, event or DRIVE_FILE_EVENT,
                             transport=transport, steps={}, s3_client=s3_client)


class UploadTests(unittest.TestCase):
    def test_downloads_via_the_connection_and_puts_the_templated_key(self):
        transport = FakeTransport()
        s3 = FakeS3()

        output = run({}, transport=transport, s3_client=s3)

        call = transport.calls[0]
        self.assertEqual(call["method"], "GET")
        self.assertEqual(call["url"],
                         "https://www.googleapis.com/drive/v3/files/1aBcD_drive_file_id?alt=media")
        self.assertEqual(call["headers"]["authorization"], "Bearer drive-token")
        self.assertEqual(call["timeout"], 30)
        body, kwargs = s3.objects[("datatalks-mailchimp-backup", "mailchimp/contacts export.csv")]
        self.assertEqual(body, b"file-bytes")
        self.assertEqual(kwargs["ContentType"], "text/csv")
        self.assertEqual(output, {"bucket": "datatalks-mailchimp-backup",
                                  "key": "mailchimp/contacts export.csv",
                                  "bytes": 10, "content_type": "text/csv"})

    def test_source_without_connection_fetches_anonymously(self):
        transport = FakeTransport()

        run({"source_connection_id": ""}, transport=transport, s3_client=FakeS3())

        self.assertNotIn("authorization", transport.calls[0]["headers"])

    def test_key_templating_sanitizes_the_file_name(self):
        s3 = FakeS3()
        event = {**DRIVE_FILE_EVENT,
                 "data": {**DRIVE_FILE_EVENT["data"], "name": "backup\\final.csv"}}

        run({}, event=event, s3_client=s3)

        self.assertIn(("datatalks-mailchimp-backup", "mailchimp/final.csv"), s3.objects)

    def test_content_type_override_wins_over_the_event_mime(self):
        s3 = FakeS3()

        run({"content_type": "application/x-backup"}, s3_client=s3)

        _, kwargs = next(iter(s3.objects.values()))
        self.assertEqual(kwargs["ContentType"], "application/x-backup")

    def test_missing_mime_falls_back_to_octet_stream(self):
        s3 = FakeS3()
        bare = {**DRIVE_FILE_EVENT,
                "data": {key: value for key, value in DRIVE_FILE_EVENT["data"].items()
                         if key != "mimeType"}}

        run({"content_type": ""}, event=bare, s3_client=s3)

        _, kwargs = next(iter(s3.objects.values()))
        self.assertEqual(kwargs["ContentType"], "application/octet-stream")

    def test_staged_s3_source_copies_without_a_download(self):
        s3 = FakeS3()
        with patch("src.dapier.engine.actions.base._s3_body", return_value=b"staged-bytes"):
            output = run({"source_url": "", "source_connection_id": "",
                          "source_s3": {"bucket": "staging", "key": "att/1"}},
                         s3_client=s3)

        body, _kwargs = s3.objects[("datatalks-mailchimp-backup", "mailchimp/contacts export.csv")]
        self.assertEqual(body, b"staged-bytes")
        self.assertEqual(output["bytes"], len(b"staged-bytes"))


class UploadErrorTests(unittest.TestCase):
    def test_both_sources_rejected(self):
        with self.assertRaises(ValueError) as caught:
            run({"source_s3": {"bucket": "staging", "key": "att/1"}})
        self.assertIn("either source_url or source_s3", str(caught.exception))

    def test_no_source_rejected(self):
        with self.assertRaises(ValueError) as caught:
            run({"source_url": "", "source_connection_id": ""})
        self.assertIn("needs source_url or source_s3", str(caught.exception))

    def test_failed_download_surfaces_the_status(self):
        with self.assertRaises(RuntimeError) as caught:
            run({}, transport=FakeTransport(status=404))
        self.assertIn("HTTP 404", str(caught.exception))

    def test_unreachable_download_fails_readable(self):
        def transport(method, url, *, headers, body, timeout=15):
            raise OSError("no network")

        with self.assertRaises(RuntimeError) as caught:
            run({}, transport=transport)
        self.assertIn("unreachable", str(caught.exception))

    def test_missing_credential_fails_before_any_download(self):
        action = {**ACTION, "source_connection_id": ""}
        with patch("src.dapier.connections.credentials.get_credential",
                   side_effect=KeyError("aws")):
            with self.assertRaises(ValueError) as caught:
                run_s3_upload(action, DRIVE_FILE_EVENT, steps={}, s3_client=FakeS3())
        self.assertIn("not configured", str(caught.exception))

    def test_credential_without_aws_keys_fails(self):
        action = {**ACTION, "source_connection_id": ""}
        with patch("src.dapier.connections.credentials.get_credential",
                   return_value={"token": "not-aws"}):
            with self.assertRaises(ValueError) as caught:
                run_s3_upload(action, DRIVE_FILE_EVENT, steps={}, s3_client=FakeS3())
        self.assertIn("does not contain AWS keys", str(caught.exception))


class RegistryDispatchTests(unittest.TestCase):
    """The registry entry is what the engine and trigger validation use."""

    def test_run_action_downloads_and_uploads(self):
        transport = FakeTransport()
        s3 = FakeS3()
        with patch("src.dapier.engine.actions.base._connected_connection",
                   return_value={"connection_id": "google-drive", "provider": "google",
                                 "status": "connected"}), \
             patch("src.dapier.engine.actions.base._default_transport", transport), \
             patch("src.dapier.connections.tokens.get_access_token",
                   return_value=("drive-token", {})), \
             patch("src.dapier.connections.credentials.get_credential",
                   return_value={"access_key_id": "AKIAEXAMPLE0000",
                                 "secret_access_key": "b" * 40}), \
             patch("boto3.client", return_value=s3):
            output = run_action(dict(ACTION), DRIVE_FILE_EVENT, "wf-1", steps={})

        self.assertEqual(output["key"], "mailchimp/contacts export.csv")
        self.assertEqual(transport.calls[0]["method"], "GET")
        self.assertEqual(
            list(s3.objects), [("datatalks-mailchimp-backup", "mailchimp/contacts export.csv")])

    def test_registry_reports_the_runnable_signature(self):
        required, optional = action_specs()["s3_upload"]
        self.assertEqual(required, frozenset({"bucket", "key"}))
        self.assertIn("source_url", optional)
        self.assertIn("source_s3", optional)
        self.assertIn("credential_id", optional)


if __name__ == "__main__":
    unittest.main()
