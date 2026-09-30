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

    def scan(self, Limit=200):
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


class TriggerApiTests(unittest.TestCase):
    def setUp(self):
        self.body = {"name": "income-2026-08", "actions": [{"type": "slack", "credential_id": "slack", "channel": "C1"}]}

    def test_save_creates_then_updates_preserving_created_fields(self):
        stub = StubTable()
        with patch.dict(os.environ, {"EMAIL_TRIGGERS_TABLE": "triggers"}):
            created_status, created = email_triggers.api_save(self.body, "op", table_ref=stub)
            self.assertEqual(created_status, 200)
            self.assertTrue(created["created"])
            self.assertEqual(created["address"], "income-2026-08@dtcdev.click")

            update = dict(self.body, description="bookkeeping", enabled=False)
            _status, updated = email_triggers.api_save(update, "op2", table_ref=stub)
        self.assertFalse(updated["created"])
        self.assertEqual(updated["description"], "bookkeeping")
        self.assertFalse(updated["enabled"])
        self.assertEqual(updated["created_by"], "op")

    def test_list_reports_triggers_domain_and_managed_routes(self):
        stub = StubTable([email_triggers.build_item(self.body, "op")])
        claimed = {"id": "invoice-pipeline", "trigger": {"connector": "email", "event": "message.received",
                                                         "filters": {"route": {"equals": "invoice-attachment"}}}}
        with patch.dict(os.environ, {"EMAIL_TRIGGERS_TABLE": "triggers"}), \
             patch("src.dapier.engine.workflows", return_value=[claimed]):
            status, payload = email_triggers.api_list(stub)
        self.assertEqual(status, 200)
        self.assertEqual(payload["domain"], "dtcdev.click")
        self.assertEqual([t["name"] for t in payload["triggers"]], ["income-2026-08"])
        self.assertEqual(payload["managed_routes"],
                         [{"name": "invoice-attachment", "workflow": "invoice-pipeline", "status": "enabled"}])

    def test_managed_routes_carry_workflow_state_and_dedupe(self):
        paused = {"id": "todo-sheet", "enabled": True, "auto_paused": True,
                  "triggers": [{"connector": "email", "filters": {"route": {"equals": "Todo"}}}]}
        off = {"id": "agents", "enabled": False,
               "trigger": {"connector": "email", "filters": {"route": {"equals": "agents"}}}}
        doubled = {"id": "todo-sheet", "enabled": True,
                   "triggers": [{"connector": "email", "filters": {"route": {"equals": "todo"}}}]}
        with patch("src.dapier.engine.workflows", return_value=[paused, off, doubled]):
            routes = email_triggers.managed_routes()
            self.assertEqual(routes, [
                {"name": "agents", "workflow": "agents", "status": "disabled"},
                {"name": "todo", "workflow": "todo-sheet", "status": "auto-paused"},
            ])
            self.assertEqual(email_triggers.yaml_email_routes(), {"agents", "todo"})

    def test_delete_missing_is_404_and_existing_is_removed(self):
        stub = StubTable([email_triggers.build_item(self.body, "op")])
        with patch.dict(os.environ, {"EMAIL_TRIGGERS_TABLE": "triggers"}):
            with self.assertRaises(email_triggers.TriggerError):
                email_triggers.api_delete("nope", "op", table_ref=stub)
            delete_status, _payload = email_triggers.api_delete("income-2026-08", "op", table_ref=stub)
        self.assertEqual(delete_status, 200)
        self.assertEqual(stub.items, {})


class WorkflowMergeTests(unittest.TestCase):
    def setUp(self):
        self.item = {
            "name": "income-2026-08",
            "address": "income-2026-08@dtcdev.click",
            "actions": [{"type": "webhook", "url": "https://hooks.test/x", "timeout_seconds": Decimal(7)}],
            "enabled": True,
        }

    def test_load_builds_route_workflow_and_decodes_decimals(self):
        workflow = email_triggers.load_workflows(table_ref=StubTable([dict(self.item)]))[0]
        self.assertEqual(workflow["id"], "email-trigger-income-2026-08")
        self.assertEqual(workflow["actions"][0]["timeout_seconds"], 7)

        event = {"connector": "email", "event": "message.received", "data": {"route": "income-2026-08"}}
        self.assertTrue(matches(workflow, event))
        self.assertFalse(matches(workflow, {"connector": "email", "event": "message.received", "data": {"route": "other"}}))

    def test_disabled_triggers_do_not_run(self):
        self.assertEqual(email_triggers.load_workflows(table_ref=StubTable([dict(self.item, enabled=False)])), [])

    def test_all_workflows_includes_stored_triggers(self):
        stub = StubTable([dict(self.item)])
        with patch.dict(os.environ, {"EMAIL_TRIGGERS_TABLE": "triggers"}), \
             patch("src.dapier.triggers.email_triggers.get_table", return_value=stub):
            merged = all_workflows()
        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[-1]["id"], "email-trigger-income-2026-08")


FLOW_BODY = {"name": "invoice-copy", "flow": "invoice-dataops"}

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

    def test_api_save_rejects_unknown_events_and_persists_watchers(self):
        stub = StubTable()
        with self.assertRaises(email_triggers.TriggerError):
            email_triggers.api_save(dict(WATCHER_BODY, event="nope"), "op", table_ref=stub)
        status, payload = email_triggers.api_save(dict(WATCHER_BODY), "op", table_ref=stub)
        self.assertEqual(status, 200)
        self.assertEqual(payload["event"], "bounce.received")
        stored = stub.items["bounce-alert"]
        self.assertEqual(stored["event"], "bounce.received")
        self.assertEqual(stored["filters"], {})
        workflow = email_triggers.load_workflows(table_ref=stub)[0]
        self.assertEqual(workflow["trigger"]["event"], "bounce.received")

    def test_update_omitting_event_keeps_the_stored_watcher(self):
        stub = StubTable()
        email_triggers.api_save(dict(WATCHER_BODY), "op", table_ref=stub)
        # the console toggle's exact body shape: no event, no filters
        _status, payload = email_triggers.api_save(
            {"name": "bounce-alert", "description": "d", "enabled": False,
             "actions": WATCHER_BODY["actions"]}, "op", table_ref=stub)
        self.assertEqual(payload["event"], "bounce.received")
        stored = stub.items["bounce-alert"]
        self.assertEqual(stored["event"], "bounce.received")
        self.assertEqual(stored["filters"], {})
        self.assertEqual(stored["address"], "")
        self.assertFalse(stored["enabled"])

    def test_update_keeps_stored_filters_unless_supplied(self):
        stub = StubTable()
        email_triggers.api_save(
            dict(WATCHER_BODY, filters={"bounce_type": {"equals": "Permanent"}}),
            "op", table_ref=stub)
        email_triggers.api_save(
            {"name": "bounce-alert", "actions": WATCHER_BODY["actions"]}, "op", table_ref=stub)
        self.assertEqual(stub.items["bounce-alert"]["filters"],
                         {"bounce_type": {"equals": "Permanent"}})
        email_triggers.api_save(
            {"name": "bounce-alert", "actions": WATCHER_BODY["actions"], "filters": {}},
            "op", table_ref=stub)
        self.assertEqual(stub.items["bounce-alert"]["filters"], {})

    def test_update_rejects_non_dict_filters_on_a_watcher(self):
        stub = StubTable()
        email_triggers.api_save(dict(WATCHER_BODY), "op", table_ref=stub)
        with self.assertRaises(email_triggers.TriggerError):
            email_triggers.api_save(
                {"name": "bounce-alert", "actions": WATCHER_BODY["actions"],
                 "filters": "gone"}, "op", table_ref=stub)

    def test_explicit_message_received_demotes_a_watcher(self):
        stub = StubTable()
        email_triggers.api_save(dict(WATCHER_BODY), "op", table_ref=stub)
        email_triggers.api_save(
            {"name": "bounce-alert", "event": "message.received",
             "actions": WATCHER_BODY["actions"]}, "op", table_ref=stub)
        stored = stub.items["bounce-alert"]
        self.assertNotIn("event", stored)
        self.assertNotIn("filters", stored)
        self.assertEqual(stored["address"], "bounce-alert@dtcdev.click")


class FlowBindingTests(unittest.TestCase):
    """Stored triggers use inline actions after the migration."""

    def test_save_uses_inline_actions(self):
        stub = StubTable()
        body = {"name": "invoice-copy", "actions": [
            {"type": "webhook", "url": "https://intake.test/x"}]}
        _status, payload = email_triggers.api_save(body, "op", table_ref=stub)
        self.assertEqual(payload["flow"], "")
        self.assertEqual(payload["actions"], body["actions"])

        workflow = email_triggers.load_workflows(table_ref=stub)[0]
        self.assertEqual(workflow["actions"], [{"type": "webhook", "url": "https://intake.test/x"}])
        event = {"connector": "email", "event": "message.received", "data": {"route": "invoice-copy"}}
        self.assertTrue(matches(workflow, event))

    def test_rejects_flow_plus_actions_and_unknown_flows(self):
        with self.assertRaises(email_triggers.TriggerError) as both:
            email_triggers.build_item(
                {"name": "invoice-x", "flow": "invoice-dataops",
                 "actions": [{"type": "webhook", "url": "https://x"}]}, "op")
        self.assertIn("not both", str(both.exception))
        with self.assertRaises(email_triggers.TriggerError) as missing:
            email_triggers.build_item({"name": "invoice-x", "flow": "nope"}, "op")
        self.assertIn("no shared flow", str(missing.exception))

    def test_list_has_no_shared_flow_catalog(self):
        stub = StubTable()
        _status, payload = email_triggers.api_list(table_ref=stub)
        self.assertEqual(payload["flows"], [])

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
    def test_operator_can_create_and_list_triggers_over_bearer_auth(self):
        stub = StubTable()
        event = {
            "headers": {},
            "body": json.dumps({
                "name": "receipts",
                "actions": [{"type": "webhook", "url": "https://hooks.test/x"}],
            }),
        }
        with patch("src.dapier.api.agent.authenticate", return_value=("sub-1", None)), \
             patch("src.dapier.api.agent.authz.is_operator", return_value=True), \
             patch("src.dapier.triggers.email_triggers.get_table", return_value=stub), \
             patch.dict(os.environ, {"EMAIL_TRIGGERS_TABLE": "triggers"}):
            created = agent_api.email_triggers_api(event, "PUT")
            listed = agent_api.email_triggers_api({"headers": {}}, "GET")

        self.assertEqual(created["statusCode"], 200)
        self.assertTrue(json.loads(created["body"])["created"])
        self.assertEqual(
            [t["name"] for t in json.loads(listed["body"])["triggers"]],
            ["receipts"],
        )

    def test_non_operator_is_rejected(self):
        with patch("src.dapier.api.agent.authenticate", return_value=("sub-2", None)), \
             patch("src.dapier.api.agent.authz.is_operator", return_value=False):
            response = agent_api.email_triggers_api({"headers": {}}, "GET")

        self.assertEqual(response["statusCode"], 403)


if __name__ == "__main__":
    unittest.main()
