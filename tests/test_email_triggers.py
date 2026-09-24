import json
import os
import unittest
from decimal import Decimal
from unittest.mock import MagicMock, patch

from src import agent_api, email_triggers
from src.engine import all_workflows, execute, matches


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
            email_triggers.validate_name("todo")
        self.assertIn("reserved", str(ctx.exception))


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
        with patch("src.email_triggers.yaml_email_routes", return_value={"taken"}):
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

    def test_list_reports_triggers_domain_and_yaml_routes(self):
        stub = StubTable([email_triggers.build_item(self.body, "op")])
        with patch.dict(os.environ, {"EMAIL_TRIGGERS_TABLE": "triggers"}):
            status, payload = email_triggers.api_list(stub)
        self.assertEqual(status, 200)
        self.assertEqual(payload["domain"], "dtcdev.click")
        self.assertEqual([t["name"] for t in payload["triggers"]], ["income-2026-08"])
        self.assertIn("invoice-attachment", payload["yaml_routes"])

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

    def test_all_workflows_appends_triggers_to_yaml(self):
        stub = StubTable([dict(self.item)])
        with patch.dict(os.environ, {"EMAIL_TRIGGERS_TABLE": "triggers"}), \
             patch("src.email_triggers.get_table", return_value=stub):
            merged = all_workflows()
        self.assertGreater(len(merged), 1)
        self.assertEqual(merged[-1]["id"], "email-trigger-income-2026-08")


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
             patch("src.email_triggers.get_table", return_value=stub), \
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
             patch("src.email_triggers.get_table", return_value=stub), \
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
        with patch("src.agent_api.authenticate", return_value=("sub-1", None)), \
             patch("src.agent_api.authz.is_operator", return_value=True), \
             patch("src.email_triggers.get_table", return_value=stub), \
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
        with patch("src.agent_api.authenticate", return_value=("sub-2", None)), \
             patch("src.agent_api.authz.is_operator", return_value=False):
            response = agent_api.email_triggers_api({"headers": {}}, "GET")

        self.assertEqual(response["statusCode"], 403)


if __name__ == "__main__":
    unittest.main()
