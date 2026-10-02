import json
import os
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path
from unittest.mock import MagicMock, patch

from src.dapier.api import agent as agent_api
from src.dapier.triggers import email_triggers
from src.dapier.engine import all_workflows, execute, matches


class StubTable:
    def __init__(self, items=None):
        self.items = {item["name"]: dict(item) for item in (items or [])}

    def scan(self, Limit=200, **kwargs):
        return {"Items": [dict(value) for value in self.items.values()]}

    def get_item(self, Key):
        item = self.items.get(Key["name"])
        return {"Item": dict(item)} if item else {}

    def put_item(self, Item):
        self.items[Item["name"]] = dict(Item)

    def delete_item(self, Key):
        self.items.pop(Key["name"], None)


class NameValidationTests(unittest.TestCase):
    def test_accepts_and_normalizes(self):
        self.assertEqual(email_triggers.validate_name(" Income-2026 "), "income-2026")

    def test_rejects_bad_names(self):
        for name in ("", "a_b", "-lead", "trail-", "x" * 40, "do it", "a"):
            with self.assertRaises(email_triggers.TriggerError):
                email_triggers.validate_name(name)

    def test_rejects_reserved(self):
        with self.assertRaises(email_triggers.TriggerError) as ctx:
            email_triggers.validate_name("postmaster")
        self.assertIn("reserved", str(ctx.exception))

    def test_routes_freed_from_yaml_are_no_longer_reserved(self):
        # invoice moved from bundled YAML to app-managed triggers; only
        # infrastructure addresses stay hard-reserved.
        self.assertEqual(email_triggers.validate_name("invoice"), "invoice")


class ActionValidationTests(unittest.TestCase):
    def test_requires_at_least_one_action(self):
        with self.assertRaises(email_triggers.TriggerError):
            email_triggers.validate_actions([])

    def test_rejects_unknown_types_and_keys(self):
        with self.assertRaises(email_triggers.TriggerError):
            email_triggers.validate_actions([{"type": "carrier-pigeon"}])
        with self.assertRaises(email_triggers.TriggerError) as ctx:
            email_triggers.validate_actions([{"type": "webhook", "url": "https://x", "bogus": 1}])
        self.assertIn("bogus", str(ctx.exception))

    def test_requires_type_keys(self):
        with self.assertRaises(email_triggers.TriggerError) as ctx:
            email_triggers.validate_actions([{"type": "dropbox_upload", "connection_id": "dropbox"}])
        self.assertIn("folder", str(ctx.exception))

    def test_accepts_email_send_and_rejects_unknown_keys(self):
        actions = [{"type": "email_send", "to": "ops@example.com",
                    "subject": "s", "text": "t", "html": "<b>t</b>", "sender": "me@dtcdev.click"}]
        self.assertEqual(email_triggers.validate_actions(actions), actions)
        with self.assertRaises(email_triggers.TriggerError):
            email_triggers.validate_actions([{"type": "email_send"}])
        with self.assertRaises(email_triggers.TriggerError) as ctx:
            email_triggers.validate_actions(
                [{"type": "email_send", "to": "ops@example.com", "bcc": "x"}])
        self.assertIn("bcc", str(ctx.exception))


class TriggerItemTests(unittest.TestCase):
    def setUp(self):
        self.body = {"name": "income-2026-08", "actions": [{"type": "dataops", "auth_secret_id": "dapier/dataops"}]}

    def test_address_uses_configured_domain(self):
        with patch.dict(os.environ, {"TRIGGER_EMAIL_DOMAIN": "dtcdev.click"}):
            item = email_triggers.build_item(dict(self.body), "op")
        self.assertEqual(item["address"], "income-2026-08@dtcdev.click")
        self.assertEqual(item["created_by"], "op")
        self.assertTrue(item["enabled"])

    def test_rejects_names_claimed_by_yaml_workflows(self):
        with patch("src.dapier.triggers.email_triggers.yaml_email_routes", return_value={"taken"}):
            with self.assertRaises(email_triggers.TriggerError) as ctx:
                email_triggers.build_item({"name": "taken", "actions": self.body["actions"]}, "op")
        self.assertIn("YAML workflow", str(ctx.exception))


WATCHER_BODY = {"name": "bounce-alert", "event": "bounce.received",
                "actions": [{"type": "webhook", "url": "https://hooks.test/x"}]}


class WatcherEventTests(unittest.TestCase):
    """SES feedback watchers: bounce/complaint events need no address — the
    name is identity only, and the engine matches on the event (optionally
    scoped by the stored filters), never on a reserved route."""

    def test_watcher_needs_no_address_and_skips_the_route_check(self):
        with patch("src.dapier.triggers.email_triggers.yaml_email_routes",
                   return_value={"bounce-alert"}):
            item = email_triggers.build_item(dict(WATCHER_BODY), "op")
        self.assertEqual(item["address"], "")
        self.assertEqual(item["event"], "bounce.received")
        self.assertEqual(item["filters"], {})

    def test_address_trigger_still_shadows_yaml_routes(self):
        with patch("src.dapier.triggers.email_triggers.yaml_email_routes",
                   return_value={"bounce-alert"}):
            with self.assertRaises(email_triggers.TriggerError) as ctx:
                email_triggers.build_item(
                    {"name": "bounce-alert",
                     "actions": WATCHER_BODY["actions"]}, "op")
        self.assertIn("YAML workflow", str(ctx.exception))

    def test_unknown_event_is_rejected(self):
        with self.assertRaises(email_triggers.TriggerError) as ctx:
            email_triggers.build_item(dict(WATCHER_BODY, event="open.tracked"), "op")
        self.assertIn("unknown email trigger event", str(ctx.exception))

    def test_watcher_filters_stored_verbatim(self):
        item = email_triggers.build_item(
            dict(WATCHER_BODY, filters={"bounce_type": {"equals": "Permanent"}}), "op")
        self.assertEqual(item["filters"], {"bounce_type": {"equals": "Permanent"}})

    def test_watcher_rejects_route_filters_and_non_dict_filters(self):
        with self.assertRaises(email_triggers.TriggerError) as route:
            email_triggers.build_item(
                dict(WATCHER_BODY, filters={"route": {"equals": "x"}}), "op")
        self.assertIn("route", str(route.exception))
        with self.assertRaises(email_triggers.TriggerError) as shape:
            email_triggers.build_item(dict(WATCHER_BODY, filters="gone"), "op")
        self.assertIn("object", str(shape.exception))

    def test_event_absent_keeps_the_legacy_item(self):
        item = email_triggers.build_item(
            {"name": "income", "actions": WATCHER_BODY["actions"]}, "op")
        self.assertNotIn("event", item)
        self.assertNotIn("filters", item)
        self.assertEqual(item["address"], "income@dtcdev.click")

    def test_explicit_message_received_keeps_the_address(self):
        item = email_triggers.build_item(
            dict(WATCHER_BODY, name="income", event="message.received"), "op")
        self.assertNotIn("event", item)
        self.assertEqual(item["address"], "income@dtcdev.click")

    def test_workflow_for_watcher_emits_stored_event_and_match_all(self):
        item = email_triggers.build_item(dict(WATCHER_BODY), "op")
        workflow = email_triggers.workflow_for(item)
        self.assertEqual(workflow["trigger"], {
            "connector": "email", "event": "bounce.received", "filters": {}})
        bounce = {"connector": "email", "event": "bounce.received",
                  "data": {"bounce_type": "Permanent", "source": "billing@example.test"}}
        self.assertTrue(matches(workflow, bounce))
        self.assertFalse(matches(workflow, {"connector": "email",
                                            "event": "message.received",
                                            "data": {"route": "bounce-alert"}}))
        self.assertFalse(matches(workflow, {"connector": "email",
                                            "event": "complaint.received",
                                            "data": {}}))

    def test_workflow_for_watcher_applies_stored_filters(self):
        item = email_triggers.build_item(
            dict(WATCHER_BODY, filters={"bounce_type": {"equals": "Permanent"}}), "op")
        workflow = email_triggers.workflow_for(item)
        self.assertEqual(workflow["trigger"]["filters"],
                         {"bounce_type": {"equals": "Permanent"}})
        self.assertTrue(matches(workflow, {"connector": "email",
                                           "event": "bounce.received",
                                           "data": {"bounce_type": "Permanent"}}))
        self.assertFalse(matches(workflow, {"connector": "email",
                                            "event": "bounce.received",
                                            "data": {"bounce_type": "Transient"}}))

    def test_public_view_shows_the_event(self):
        watcher = email_triggers.build_item(dict(WATCHER_BODY), "op")
        self.assertEqual(email_triggers.public_view(watcher)["event"], "bounce.received")
        legacy = email_triggers.build_item(
            {"name": "income", "actions": WATCHER_BODY["actions"]}, "op")
        self.assertEqual(email_triggers.public_view(legacy)["event"], "message.received")







class FlowBindingTests(unittest.TestCase):
    """Stored triggers use inline actions after the migration."""


    def test_rejects_flow_plus_actions_and_unknown_flows(self):
        with self.assertRaises(email_triggers.TriggerError) as both:
            email_triggers.build_item(
                {"name": "invoice-x", "flow": "invoice-dataops",
                 "actions": [{"type": "webhook", "url": "https://x"}]}, "op")
        self.assertIn("not both", str(both.exception))
        with self.assertRaises(email_triggers.TriggerError) as missing:
            email_triggers.build_item({"name": "invoice-x", "flow": "nope"}, "op")
        self.assertIn("no shared flow", str(missing.exception))


    def test_unmigrated_flow_bound_trigger_fails_closed(self):
        stub = StubTable([{"name": "invoice-copy", "flow": "invoice-dataops",
                           "actions": [], "enabled": True}])
        self.assertEqual(email_triggers.load_workflows(table_ref=stub), [])


class FakeResponse:
    status = 200

    def read(self):
        return b""

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False


class ExecuteEndToEndTests(unittest.TestCase):
    def test_email_to_trigger_address_runs_its_actions(self):
        stub = StubTable([{
            "name": "receipts",
            "address": "receipts@dtcdev.click",
            "actions": [{"type": "webhook", "url": "https://hooks.test/x"}],
            "enabled": True,
        }])
        event = {
            "schema_version": "1.0",
            "id": "email:<msg-1>",
            "connector": "email",
            "event": "message.received",
            "data": {"route": "receipts", "subject": "hi"},
        }
        with patch.dict(os.environ, {"EMAIL_TRIGGERS_TABLE": "triggers"}), \
             patch("src.dapier.triggers.email_triggers.get_table", return_value=stub), \
             patch("urllib.request.urlopen", return_value=FakeResponse()) as urlopen:
            execute(event)

        self.assertEqual(urlopen.call_count, 1)
        request = urlopen.call_args.args[0]
        self.assertEqual(request.full_url, "https://hooks.test/x")
        self.assertIn(b'"route":"receipts"', request.data)

    def test_unmatched_address_runs_nothing(self):
        stub = StubTable([{
            "name": "receipts",
            "address": "receipts@dtcdev.click",
            "actions": [{"type": "webhook", "url": "https://hooks.test/x"}],
            "enabled": True,
        }])
        event = {"connector": "email", "event": "message.received", "data": {"route": "stranger"}}
        with patch.dict(os.environ, {"EMAIL_TRIGGERS_TABLE": "triggers"}), \
             patch("src.dapier.triggers.email_triggers.get_table", return_value=stub), \
             patch("urllib.request.urlopen") as urlopen:
            execute(event)

        urlopen.assert_not_called()


class AgentApiTests(unittest.TestCase):
    def test_operator_reads_inventory_but_cannot_write_legacy_flows(self):
        payload = {"domain": "dtcdev.click", "addresses": []}
        with patch("src.dapier.api.agent.authenticate", return_value=("sub-1", None)), \
             patch("src.dapier.api.agent.authz.is_operator", return_value=True), \
             patch("src.dapier.triggers.email_routes.inventory", return_value=payload):
            created = agent_api.email_triggers_api({"headers": {}, "body": "{}"}, "PUT")
            listed = agent_api.email_triggers_api({"headers": {}}, "GET")
        self.assertEqual(created["statusCode"], 410)
        self.assertEqual(json.loads(listed["body"]), payload)

    def test_non_operator_is_rejected(self):
        with patch("src.dapier.api.agent.authenticate", return_value=("sub-2", None)), \
             patch("src.dapier.api.agent.authz.is_operator", return_value=False):
            response = agent_api.email_triggers_api({"headers": {}}, "GET")

        self.assertEqual(response["statusCode"], 403)


if __name__ == "__main__":
    unittest.main()
