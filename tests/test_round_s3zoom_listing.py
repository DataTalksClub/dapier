"""Listing-round tests: s3_find's match list and pagination, and the new
zoom_list_past_participants.

Unit tests drive each registered runner with a fake boto3 S3 client (s3.py's
seam) or a fake provider transport (the test_round_yts3zoom pattern) and the
connection/token/credential seams patched — no network. The s3 half pins the
back-compat contract (``found``/``key``/``size``/``last_modified``/``bucket``
stay, with ``key`` the first match), the 25-entry ``matches`` cap, and the
``next_token`` round-trip into a repeated run's ContinuationToken. The zoom
half covers the one-page happy path, next_page_token pagination to
exhaustion, the participant cap, and the empty-meeting_id error. The
registry half checks both types' field specs, the new fields' help text, and
that a chain using them validates.
"""
import json
import unittest
from unittest.mock import patch

from src.dapier.connectors import registry
from src.dapier.engine.actions.s3 import run_s3_find
from src.dapier.engine.actions.zoom import run_zoom_list_past_participants

import src.dapier.connectors  # noqa: F401  (import = registration)


ZOOM_CONNECTION = {"connection_id": "zoom-main", "provider": "zoom",
                   "status": "connected", "credential_id": "oauth#zoom-main"}
AWS_KEYS = {"access_key_id": "AKIAEXAMPLE0000", "secret_access_key": "b" * 40}

EVENT = {"id": "evt/1", "connector": "schedule", "event": "schedule.fired",
         "occurred_at": "2026-09-28T09:00:00+00:00",
         "data": {"meeting_id": "9100", "topic": "Standup"}}


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


class PagedTransport:
    """Hands out queued (status, body) pages in call order, recording URLs."""

    def __init__(self, *pages):
        self.pages = list(pages)
        self.calls = []

    def __call__(self, method, url, *, headers=None, body=None, timeout=15):
        self.calls.append({"method": method, "url": url, "headers": headers,
                           "body": body, "timeout": timeout})
        if len(self.calls) > len(self.pages):
            raise AssertionError("unexpected extra provider call")
        return self.pages[len(self.calls) - 1]


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


def json_body(payload):
    return json.dumps(payload).encode()


# --- s3_find: match list + pagination -------------------------------------------


def run_find(overrides=None, *, s3_client=None, steps=None):
    action = {"type": "s3_find", "bucket": "backups", "pattern": "report.pdf",
              **(overrides or {})}
    with patch("src.dapier.connections.credentials.get_credential",
               return_value=dict(AWS_KEYS)):
        return run_s3_find(action, {"data": {}}, steps=steps, s3_client=s3_client)


class S3FindListingTests(unittest.TestCase):
    def test_first_match_keeps_the_single_object_output(self):
        s3 = FakeS3(page([
            {"Key": "reports/other.pdf", "Size": 1},
            {"Key": "reports/2026/report.pdf", "Size": 1234,
             "LastModified": "2026-09-26T21:03:49+00:00"},
            {"Key": "reports/2025/report.pdf", "Size": 7},
        ]))

        output = run_find(s3_client=s3)

        self.assertTrue(output["found"])
        self.assertEqual(output["key"], "reports/2026/report.pdf")
        self.assertEqual(output["size"], 1234)
        self.assertEqual(output["last_modified"], "2026-09-26T21:03:49+00:00")
        self.assertEqual(output["bucket"], "backups")
        self.assertEqual(output["matches"], [
            {"key": "reports/2026/report.pdf", "size": 1234,
             "last_modified": "2026-09-26T21:03:49+00:00"},
            {"key": "reports/2025/report.pdf", "size": 7, "last_modified": None},
        ])
        self.assertEqual(output["next_token"], "")  # listing exhausted
        self.assertEqual(len(s3.calls), 1)

    def test_matches_are_bounded_at_25_and_keep_the_walk_alive(self):
        keys = [{"Key": f"reports/batch-{i:02d}.pdf", "Size": i}
                for i in range(30)]
        s3 = FakeS3(page(keys, truncated=True, token="tok-2"))

        output = run_find({"match": "prefix", "pattern": "reports/"},
                          s3_client=s3)

        self.assertEqual(len(output["matches"]), 25)
        self.assertEqual(output["key"], "reports/batch-00.pdf")
        self.assertEqual(output["matches"][-1]["key"], "reports/batch-24.pdf")
        self.assertEqual(output["next_token"], "tok-2")
        self.assertEqual(len(s3.calls), 1)  # the cap stops the walk

    def test_next_token_round_trips_into_the_next_find(self):
        # The walk follows tokens itself for up to FIND_MAX_PAGES pages, so
        # the token only rides out when the page budget is exhausted with
        # the listing still truncated.
        first = FakeS3(
            page([{"Key": "a/other.pdf", "Size": 1}], truncated=True,
                 token="tok-2"),
            page([{"Key": "b/other.pdf", "Size": 1}], truncated=True,
                 token="tok-3"),
            page([{"Key": "c/other.pdf", "Size": 1}], truncated=True,
                 token="tok-4"),
        )

        output = run_find(s3_client=first)

        self.assertFalse(output["found"])
        self.assertEqual(output["next_token"], "tok-4")
        self.assertEqual(len(first.calls), 3)
        self.assertNotIn("ContinuationToken", first.calls[0])

        second = FakeS3(page([{"Key": "d/report.pdf", "Size": 2}]))
        output = run_find({"next_token": "{steps.find.output.next_token}"},
                          s3_client=second,
                          steps={"find": {"output": {"next_token": "tok-4"}}})

        self.assertEqual(second.calls[0]["ContinuationToken"], "tok-4")
        self.assertTrue(output["found"])
        self.assertEqual(output["key"], "d/report.pdf")
        self.assertEqual(output["next_token"], "")  # exhausted now

    def test_miss_keeps_found_false_with_empty_matches(self):
        s3 = FakeS3(page([{"Key": "reports/2026/other.pdf", "Size": 9}]))

        output = run_find(s3_client=s3)

        self.assertEqual(output, {"found": False, "key": None,
                                  "matches": [], "next_token": ""})
        self.assertEqual(len(s3.calls), 1)


# --- zoom_list_past_participants ------------------------------------------------


def run_participants(transport, action, event=None, steps=None):
    action = {"type": "zoom_list_past_participants",
              "connection_id": "zoom-main", **action}
    with patch("src.dapier.engine.actions.zoom._zoom_connection",
               return_value=dict(ZOOM_CONNECTION)), \
         patch("src.dapier.connections.tokens.get_access_token",
               return_value=("tok", {})):
        return run_zoom_list_past_participants(action, event or EVENT,
                                               steps=steps, transport=transport)


class ZoomListPastParticipantsTests(unittest.TestCase):
    def test_lists_one_page_and_trims_the_projection(self):
        transport = FakeTransport(
            ("past_meetings", 200, json_body({
                "page_count": 1, "total_records": 1,
                "participants": [{
                    "id": "p-1", "user_id": "u-1", "name": "Ada Lovelace",
                    "user_email": "ada@example.test",
                    "join_time": "2026-09-27T10:00:03Z",
                    "leave_time": "2026-09-27T11:00:10Z", "duration": 3607,
                    "attentiveness_score": "98",  # API noise, trimmed away
                }]})))

        output = run_participants(transport, {"meeting_id": "9100"})

        self.assertEqual(output, {
            "participants": [{"name": "Ada Lovelace",
                              "user_email": "ada@example.test",
                              "join_time": "2026-09-27T10:00:03Z",
                              "leave_time": "2026-09-27T11:00:10Z",
                              "duration": 3607}],
            "count": 1, "meeting_id": "9100"})
        call, = transport.calls
        self.assertEqual(call["method"], "GET")
        self.assertEqual(
            call["url"],
            "https://api.zoom.us/v2/past_meetings/9100/participants?per_page=300")
        self.assertEqual(call["headers"]["authorization"], "Bearer tok")

    def test_meeting_id_takes_a_template(self):
        transport = PagedTransport((200, json_body({"participants": []})))

        output = run_participants(transport, {"meeting_id": "{trigger.meeting_id}"})

        self.assertEqual(output, {"participants": [], "count": 0,
                                  "meeting_id": "9100"})
        self.assertEqual(
            transport.calls[0]["url"],
            "https://api.zoom.us/v2/past_meetings/9100/participants?per_page=300")

    def test_follows_next_page_token_until_exhausted(self):
        transport = PagedTransport(
            (200, json_body({"participants": [
                {"name": "Ada", "user_email": "ada@example.test"},
                {"name": "Grace", "user_email": "grace@example.test"}],
                "next_page_token": "np-2"})),
            (200, json_body({"participants": [
                {"name": "Alan", "user_email": "alan@example.test"}]})))

        output = run_participants(transport, {"meeting_id": "uuid-9"})

        self.assertEqual(output["count"], 3)
        self.assertEqual([p["name"] for p in output["participants"]],
                         ["Ada", "Grace", "Alan"])
        self.assertEqual(len(transport.calls), 2)
        self.assertIn("per_page=300", transport.calls[1]["url"])
        self.assertIn("next_page_token=np-2", transport.calls[1]["url"])

    def test_stops_at_the_participant_cap_even_when_zoom_has_more(self):
        transport = PagedTransport(
            (200, json_body({"participants": [{"name": f"p{i}"}
                                              for i in range(300)],
                             "next_page_token": "np-2"})))

        output = run_participants(transport, {"meeting_id": "9100"})

        self.assertEqual(output["count"], 300)
        self.assertEqual(len(transport.calls), 1)  # the cap stops the walk

    def test_empty_meeting_id_is_a_clear_error_before_any_call(self):
        transport = PagedTransport()

        with self.assertRaises(ValueError) as caught:
            run_participants(transport, {})

        self.assertIn("requires meeting_id", str(caught.exception))
        self.assertEqual(transport.calls, [])

    def test_zoom_error_surfaces_status_and_message(self):
        transport = FakeTransport(
            ("past_meetings", 404, json_body({"message": "Meeting not found"})))

        with self.assertRaises(RuntimeError) as caught:
            run_participants(transport, {"meeting_id": "9100"})

        self.assertIn("HTTP 404", str(caught.exception))
        self.assertIn("Meeting not found", str(caught.exception))


# --- registry wiring ------------------------------------------------------------


class RegistryTests(unittest.TestCase):
    def test_new_and_updated_actions_carry_their_specs(self):
        specs = registry.action_specs()
        self.assertEqual(specs["zoom_list_past_participants"],
                         ({"connection_id", "meeting_id"}, frozenset()))
        self.assertEqual(specs["s3_find"],
                         ({"bucket", "pattern"},
                          {"prefix", "match", "next_token",
                           "credential_id", "connection_id"}))

    def test_field_schema(self):
        fields = {field["key"]: field
                  for field in registry.ACTIONS["zoom_list_past_participants"].fields}
        self.assertEqual(fields["meeting_id"].get("discover"),
                         {"resource": "zoom.past_meetings"})
        self.assertIn("meeting.ended", fields["meeting_id"].get("help", ""))
        find_fields = {field["key"]: field
                       for field in registry.ACTIONS["s3_find"].fields}
        self.assertIn("continuation token",
                      find_fields["next_token"].get("help", ""))

    def test_a_chain_using_the_new_actions_validates(self):
        registry.validate_action_chain([
            {"type": "zoom_list_past_participants", "connection_id": "zoom-main",
             "meeting_id": "{trigger.uuid}"},
            {"type": "s3_find", "bucket": "backups",
             "pattern": "{trigger.topic}.csv",
             "next_token": "{steps.find.output.next_token}"},
        ])


if __name__ == "__main__":
    unittest.main()
