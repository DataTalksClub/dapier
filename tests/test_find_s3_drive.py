"""s3_find and drive_find_file: match modes, basename fallback, pagination,
and the found-False-instead-of-raising miss handling."""
import json
import unittest
import urllib.parse
from datetime import datetime, timezone
from unittest.mock import patch

from src.dapier.connectors.registry import DISCOVERIES, action_specs, run_action
from src.dapier.engine.actions.drive import run_drive_find_file
from plugins.aws.runners.s3 import run_s3_find


class FakeTransport:
    """Route provider calls by URL substring to canned JSON responses."""

    def __init__(self, *routes):
        self.routes = routes  # (substring, status, payload) tuples
        self.calls = []

    def __call__(self, method, url, *, headers=None, body=None, timeout=15):
        self.calls.append({"method": method, "url": url, "headers": headers,
                           "body": body, "timeout": timeout})
        for substring, status, payload in self.routes:
            if substring in url:
                return status, json.dumps(payload).encode()
        raise AssertionError(f"unexpected provider call: {method} {url}")


def page(contents, *, truncated=False, token=None):
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


def run_s3_find_with(overrides=None, *, s3_client=None):
    action = {"type": "s3_find", "bucket": "backups", "pattern": "report.pdf",
              **(overrides or {})}
    with patch("src.dapier.connections.credentials.get_credential",
               return_value={"access_key_id": "AKIAEXAMPLE0000",
                             "secret_access_key": "b" * 40}):
        return run_s3_find(action, {"data": {}}, steps={}, s3_client=s3_client)


def run_drive_find_with(overrides=None, *, transport=None):
    action = {"type": "drive_find_file", "connection_id": "google",
              "name": "report.pdf", **(overrides or {})}
    with patch("src.dapier.engine.actions.base._connected_connection",
               return_value={"connection_id": "google", "provider": "google",
                             "status": "connected"}), \
         patch("src.dapier.connections.tokens.get_access_token",
               return_value=("tok", {})):
        return run_drive_find_file(action, {"data": {}}, steps={},
                                   transport=transport)


class S3FindTests(unittest.TestCase):
    def test_exact_match_on_the_full_key_wins(self):
        s3 = FakeS3(page([
            {"Key": "reports/2025/summary.pdf", "Size": 1},
            {"Key": "reports/2026/report.pdf", "Size": 1234,
             "LastModified": "2026-09-26T21:03:49+00:00"},
        ]))

        output = run_s3_find_with(s3_client=s3)

        self.assertEqual(output, {"found": True, "key": "reports/2026/report.pdf",
                                  "size": 1234,
                                  "last_modified": "2026-09-26T21:03:49+00:00",
                                  "bucket": "backups",
                                  "matches": [{"key": "reports/2026/report.pdf",
                                               "size": 1234,
                                               "last_modified": "2026-09-26T21:03:49+00:00"}],
                                  "next_token": ""})
        self.assertEqual(s3.calls[0], {"Bucket": "backups", "Prefix": "",
                                       "MaxKeys": 1000})

    def test_basename_fallback_finds_without_the_folder_path(self):
        s3 = FakeS3(page([{"Key": "reports/2026/report.pdf", "Size": 9}]))

        output = run_s3_find_with(s3_client=s3)

        self.assertEqual(output["found"], True)
        self.assertEqual(output["key"], "reports/2026/report.pdf")

    def test_contains_matches_anywhere_in_the_key(self):
        s3 = FakeS3(page([{"Key": "data/q3-financials.csv", "Size": 3}]))

        output = run_s3_find_with({"pattern": "financials", "match": "contains"},
                                  s3_client=s3)

        self.assertEqual(output["key"], "data/q3-financials.csv")

    def test_prefix_and_suffix_modes(self):
        prefix_hit = FakeS3(page([{"Key": "reports/2026/report.pdf", "Size": 9}]))
        self.assertEqual(
            run_s3_find_with({"pattern": "reports/", "match": "prefix"},
                             s3_client=prefix_hit)["found"], True)
        suffix_hit = FakeS3(page([{"Key": "reports/2026/report.PDF", "Size": 9}]))
        self.assertEqual(
            run_s3_find_with({"pattern": ".PDF", "match": "suffix"},
                             s3_client=suffix_hit)["found"], True)

    def test_no_match_is_found_false_not_an_error(self):
        s3 = FakeS3(page([{"Key": "reports/2026/other.pdf", "Size": 9}]))

        output = run_s3_find_with(s3_client=s3)

        self.assertEqual(output, {"found": False, "key": None,
                                  "matches": [], "next_token": ""})
        self.assertEqual(len(s3.calls), 1)

    def test_empty_bucket_is_found_false(self):
        s3 = FakeS3(page([]))

        output = run_s3_find_with(s3_client=s3)

        self.assertEqual(output, {"found": False, "key": None,
                                  "matches": [], "next_token": ""})

    def test_pagination_follows_the_continuation_token(self):
        s3 = FakeS3(
            page([{"Key": "a/other.pdf", "Size": 1}],
                 truncated=True, token="tok-2"),
            page([{"Key": "a/report.pdf", "Size": 2}]))

        output = run_s3_find_with(s3_client=s3)

        self.assertEqual(output["key"], "a/report.pdf")
        self.assertEqual(len(s3.calls), 2)
        self.assertNotIn("ContinuationToken", s3.calls[0])
        self.assertEqual(s3.calls[1]["ContinuationToken"], "tok-2")

    def test_rendered_pattern_and_prefix_reach_the_listing(self):
        s3 = FakeS3(page([{"Key": "reports/2026/report.pdf", "Size": 9}]))

        run_s3_find_with({"pattern": "{name}.pdf", "prefix": "reports/"},
                         s3_client=s3)

        self.assertEqual(s3.calls[0]["Prefix"], "reports/")

    def test_missing_bucket_fails_before_the_listing(self):
        with self.assertRaises(ValueError) as caught:
            run_s3_find_with({"bucket": ""}, s3_client=FakeS3(page([])))
        self.assertIn("requires a bucket", str(caught.exception))

    def test_missing_pattern_fails(self):
        with self.assertRaises(ValueError) as caught:
            run_s3_find_with({"pattern": ""}, s3_client=FakeS3(page([])))
        self.assertIn("requires a pattern", str(caught.exception))

    def test_unknown_match_mode_fails(self):
        with self.assertRaises(ValueError) as caught:
            run_s3_find_with({"match": "regex"}, s3_client=FakeS3(page([])))
        self.assertIn("exact, prefix, suffix, contains", str(caught.exception))


class DriveFindTests(unittest.TestCase):
    def test_contains_is_the_default_and_returns_the_first_file(self):
        transport = FakeTransport(
            ("drive/v3/files", 200, {"files": [
                {"id": "f1", "name": "report 2026.pdf", "mimeType": "application/pdf",
                 "modifiedTime": "2026-09-26T21:03:49.887Z"},
                {"id": "f2", "name": "report 2025.pdf"},
            ]}))

        output = run_drive_find_with(transport=transport)

        self.assertEqual(output, {"found": True, "count": 2, "file": {
            "id": "f1", "name": "report 2026.pdf",
            "mimeType": "application/pdf",
            "modified": "2026-09-26T21:03:49.887Z"},
            "files": [
                {"id": "f1", "name": "report 2026.pdf",
                 "mimeType": "application/pdf",
                 "modified": "2026-09-26T21:03:49.887Z"},
                {"id": "f2", "name": "report 2025.pdf",
                 "mimeType": None, "modified": None}],
            "next_page_token": None})
        call = transport.calls[0]
        self.assertEqual(call["method"], "GET")
        self.assertEqual(call["headers"]["authorization"], "Bearer tok")
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(call["url"]).query)
        self.assertEqual(query["q"],
                         ["trashed=false and name contains 'report.pdf'"])
        self.assertEqual(query["pageSize"], ["10"])
        self.assertEqual(query["orderBy"], ["modifiedTime desc"])
        self.assertEqual(query["fields"], ["files(id,name,mimeType,modifiedTime)"])
        self.assertEqual(query["supportsAllDrives"], ["true"])

    def test_exact_mode_builds_an_equality_query(self):
        transport = FakeTransport(
            ("drive/v3/files", 200, {"files": [
                {"id": "f1", "name": "report.pdf"}]}))

        run_drive_find_with({"match": "exact", "name": "report 2026.pdf"},
                            transport=transport)

        query = urllib.parse.parse_qs(
            urllib.parse.urlsplit(transport.calls[0]["url"]).query)
        self.assertEqual(query["q"], ["trashed=false and name = 'report 2026.pdf'"])

    def test_single_quotes_are_escaped_for_the_drive_query(self):
        transport = FakeTransport(("drive/v3/files", 200, {"files": []}))

        run_drive_find_with({"name": "o'brien's report"}, transport=transport)

        query = urllib.parse.parse_qs(
            urllib.parse.urlsplit(transport.calls[0]["url"]).query)
        self.assertEqual(query["q"],
                         ["trashed=false and name contains 'o\\'brien\\'s report'"])

    def test_no_match_is_found_false_not_an_error(self):
        transport = FakeTransport(("drive/v3/files", 200, {"files": []}))

        output = run_drive_find_with(transport=transport)

        self.assertEqual(output, {"found": False, "file": None, "count": 0,
                                  "files": [], "next_page_token": None})

    def test_missing_files_key_is_found_false(self):
        transport = FakeTransport(("drive/v3/files", 200, {}))

        output = run_drive_find_with(transport=transport)

        self.assertEqual(output, {"found": False, "file": None, "count": 0,
                                  "files": [], "next_page_token": None})

    def test_http_error_surfaces_the_status_and_detail(self):
        transport = FakeTransport(
            ("drive/v3/files", 403, {"error": {"message": "no drive scope"}}))

        with self.assertRaises(RuntimeError) as caught:
            run_drive_find_with(transport=transport)
        self.assertIn("HTTP 403", str(caught.exception))
        self.assertIn("no drive scope", str(caught.exception))

    def test_missing_name_fails(self):
        with self.assertRaises(ValueError) as caught:
            run_drive_find_with({"name": ""}, transport=FakeTransport())
        self.assertIn("requires a name", str(caught.exception))

    def test_folder_narrows_the_search_to_that_parent(self):
        transport = FakeTransport(("drive/v3/files", 200, {"files": []}))

        run_drive_find_with({"folder": "folder-9"}, transport=transport)

        query = urllib.parse.parse_qs(
            urllib.parse.urlsplit(transport.calls[0]["url"]).query)
        self.assertEqual(
            query["q"],
            ["trashed=false and name contains 'report.pdf' and 'folder-9' in parents"])

    def test_folder_is_templated_and_optional(self):
        transport = FakeTransport(("drive/v3/files", 200, {"files": []}))

        run_drive_find_with({"folder": "{data.folder_id}"}, transport=transport)
        query = urllib.parse.parse_qs(
            urllib.parse.urlsplit(transport.calls[0]["url"]).query)
        self.assertNotIn(" in parents", query["q"][0])

        run_drive_find_with({"folder": "{data.folder_id}",
                             "name": "o'brien's report"}, transport=transport)
        query = urllib.parse.parse_qs(
            urllib.parse.urlsplit(transport.calls[1]["url"]).query)
        # The template renders empty against this event, so no parent clause
        # lands; the name's quotes are still escaped.
        self.assertEqual(query["q"],
                         ["trashed=false and name contains 'o\\'brien\\'s report'"])


class RegistryTests(unittest.TestCase):
    """The registry entries are what the engine and trigger validation use."""

    CREDENTIAL = {"access_key_id": "AKIAEXAMPLE0000", "secret_access_key": "b" * 40}

    def test_run_action_finds_through_the_registered_runner(self):
        s3 = FakeS3(page([{"Key": "report.pdf", "Size": 5}]))
        with patch("src.dapier.connections.credentials.get_credential",
                   return_value=self.CREDENTIAL), \
             patch("boto3.client", return_value=s3):
            output = run_action(
                {"type": "s3_find", "bucket": "backups", "pattern": "report.pdf"},
                {"data": {}}, "wf-1", steps={})

        self.assertEqual(output, {"found": True, "key": "report.pdf", "size": 5,
                                  "last_modified": None, "bucket": "backups",
                                  "matches": [{"key": "report.pdf", "size": 5,
                                               "last_modified": None}],
                                  "next_token": ""})

    def test_run_action_searches_drive_through_the_default_transport(self):
        transport = FakeTransport(
            ("drive/v3/files", 200, {"files": [
                {"id": "f1", "name": "report.pdf", "mimeType": "application/pdf",
                 "modifiedTime": "2026-09-26T21:03:49.887Z"}]}))
        with patch("src.dapier.engine.actions.base._connected_connection",
                   return_value={"connection_id": "google", "provider": "google",
                                 "status": "connected"}), \
             patch("src.dapier.engine.actions.base._default_transport", transport), \
             patch("src.dapier.connections.tokens.get_access_token",
                   return_value=("tok", {})):
            output = run_action(
                {"type": "drive_find_file", "connection_id": "google",
                 "name": "report.pdf"}, {"data": {}}, "wf-1", steps={})

        self.assertEqual(output["found"], True)
        self.assertEqual(output["file"]["id"], "f1")

    def test_registry_reports_the_runnable_signatures(self):
        required, optional = action_specs()["s3_find"]
        self.assertEqual(required, frozenset({"bucket", "pattern"}))
        self.assertEqual(
            optional, frozenset({"prefix", "match", "next_token",
                                 "credential_id", "connection_id"}))
        required, optional = action_specs()["drive_find_file"]
        self.assertEqual(required, frozenset({"connection_id", "name"}))
        self.assertEqual(optional, frozenset({"match", "folder"}))

    def test_s3_objects_discovery_lists_bucket_keys(self):
        s3 = FakeS3(page([{"Key": "reports/2026/report.pdf", "Size": 9,
                           "LastModified": datetime(
                               2026, 9, 26, 21, 3, 49, tzinfo=timezone.utc)}]))
        with patch("src.dapier.connections.credentials.get_credential",
                   return_value=self.CREDENTIAL), \
             patch("boto3.client", return_value=s3):
            items = DISCOVERIES["s3.objects"].run(
                {"credential_id": "aws"}, {"bucket": "backups"})

        self.assertEqual(items, [{"id": "reports/2026/report.pdf",
                                  "name": "report.pdf", "size": 9,
                                  "modified": "2026-09-26T21:03:49+00:00"}])
        self.assertEqual(s3.calls[0]["Bucket"], "backups")
        self.assertEqual(s3.calls[0]["MaxKeys"], 100)


if __name__ == "__main__":
    unittest.main()
