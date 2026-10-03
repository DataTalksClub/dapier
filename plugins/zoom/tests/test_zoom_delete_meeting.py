"""Tests for the zoom_delete_meeting action staple: DELETE /meetings/{id},
with the optional occurrence_id that scopes a recurring meeting's delete to
one occurrence.

Unit tests drive the registered runner with a fake provider transport and
the connection/token seams patched (the test_round_yts3zoom pattern),
asserting method, URL, and the step-output shape; the registry half checks
the action's field specs, that validate_action_chain accepts a chain using
it beside its sibling meeting actions, and rejects chains missing its
required fields or carrying unknown keys.
"""
import json
import unittest
from unittest.mock import patch

from src.dapier.connectors import registry
from plugins.zoom.runners import run_zoom_delete_meeting

import src.dapier.connectors  # noqa: F401  (import = registration)


ZOOM_CONNECTION = {"connection_id": "zoom-main", "provider": "zoom",
                   "status": "connected", "credential_id": "oauth#zoom-main"}

EVENT = {"id": "evt/1", "connector": "schedule", "event": "schedule.fired",
         "occurred_at": "2026-09-28T09:00:00+00:00",
         "data": {"meeting_id": "9100", "occurrence_id": ""}}


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


def run_delete(transport, action, event=None, steps=None):
    action = {"type": "zoom_delete_meeting", "connection_id": "zoom-main", **action}
    with patch("plugins.zoom.runners._zoom_connection",
               return_value=dict(ZOOM_CONNECTION)), \
         patch("src.dapier.connections.tokens.get_access_token",
               return_value=("tok", {})):
        return run_zoom_delete_meeting(action, event or EVENT, steps=steps,
                                       transport=transport)


class ZoomDeleteMeetingTests(unittest.TestCase):
    def test_deletes_the_meeting_and_names_it(self):
        transport = FakeTransport(("/meetings/9100", 204, b""))

        output = run_delete(transport, {"meeting_id": "9100"})

        self.assertEqual(output, {"deleted": True, "meeting_id": "9100"})
        call, = transport.calls
        self.assertEqual(call["method"], "DELETE")
        self.assertEqual(call["url"], "https://api.zoom.us/v2/meetings/9100")
        self.assertEqual(call["headers"]["authorization"], "Bearer tok")
        self.assertIsNone(call["body"])  # a delete carries no body

    def test_occurrence_id_scopes_a_recurring_delete(self):
        transport = FakeTransport(("/meetings/9100", 204, b""))

        output = run_delete(transport, {"meeting_id": "9100",
                                        "occurrence_id": "oc-1"})

        self.assertEqual(output, {"deleted": True, "meeting_id": "9100",
                                  "occurrence_id": "oc-1"})
        call, = transport.calls
        self.assertEqual(
            call["url"],
            "https://api.zoom.us/v2/meetings/9100?occurrence_id=oc-1")

    def test_ids_take_templates(self):
        transport = FakeTransport(("/meetings/9100", 204, b""))

        output = run_delete(
            transport,
            {"meeting_id": "{steps.find.output.meeting_id}",
             "occurrence_id": "{steps.find.output.occurrence_id}"},
            steps={"find": {"output": {"meeting_id": "9100",
                                       "occurrence_id": "oc-2"}}})

        self.assertEqual(output["meeting_id"], "9100")
        self.assertEqual(
            transport.calls[0]["url"],
            "https://api.zoom.us/v2/meetings/9100?occurrence_id=oc-2")

    def test_zoom_error_surfaces_status_and_message(self):
        transport = FakeTransport(
            ("/meetings/9100", 404, json_body({"message": "Meeting not found"})))

        with self.assertRaises(RuntimeError) as caught:
            run_delete(transport, {"meeting_id": "9100"})
        self.assertIn("HTTP 404", str(caught.exception))
        self.assertIn("Meeting not found", str(caught.exception))

    def test_requires_meeting_id(self):
        transport = FakeTransport()

        with self.assertRaises(ValueError):
            run_delete(transport, {})
        self.assertEqual(transport.calls, [])

    def test_registry_dispatch_deletes(self):
        transport = FakeTransport(("/meetings/9100", 204, b""))
        with patch("src.dapier.engine.actions.base._default_transport",
                   transport), \
             patch("plugins.zoom.runners._zoom_connection",
                   return_value=dict(ZOOM_CONNECTION)), \
             patch("src.dapier.connections.tokens.get_access_token",
                   return_value=("tok", {})):
            output = registry.run_action(
                {"type": "zoom_delete_meeting", "connection_id": "zoom-main",
                 "meeting_id": "9100"},
                EVENT, "wf-1", steps={})

        self.assertEqual(output, {"deleted": True, "meeting_id": "9100"})


class RegistryTests(unittest.TestCase):
    def test_registered_with_its_specs(self):
        self.assertEqual(
            registry.action_specs()["zoom_delete_meeting"],
            ({"connection_id", "meeting_id"}, {"occurrence_id"}))

    def test_field_schema_matches_the_meeting_actions(self):
        fields = {field["key"]: field
                  for field in registry.ACTIONS["zoom_delete_meeting"].fields}
        self.assertEqual(fields["meeting_id"].get("discover"),
                         {"resource": "zoom.past_meetings"})
        self.assertIn("ecurring", fields["occurrence_id"].get("help", ""))

    def test_a_chain_using_the_new_action_validates(self):
        registry.validate_action_chain([
            {"type": "zoom_find_meeting", "connection_id": "zoom-main",
             "meeting_id": "9100"},
            {"type": "zoom_delete_meeting", "connection_id": "zoom-main",
             "meeting_id": "{steps.find.output.meeting.id}"},
            {"type": "zoom_delete_meeting", "connection_id": "zoom-main",
             "meeting_id": "9200", "occurrence_id": "oc-3"},
        ])

    def test_validation_rejects_missing_and_unknown(self):
        with self.assertRaises(registry.ActionError) as caught:
            registry.validate_action_chain([
                {"type": "zoom_delete_meeting", "connection_id": "zoom-main"}])
        self.assertIn("missing: meeting_id", str(caught.exception))
        with self.assertRaises(registry.ActionError) as caught:
            registry.validate_action_chain([
                {"type": "zoom_delete_meeting", "connection_id": "zoom-main",
                 "meeting_id": "9100", "topic": "Renamed"}])
        self.assertIn("unknown keys: topic", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
