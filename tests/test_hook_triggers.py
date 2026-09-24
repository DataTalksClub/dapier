"""Tests for webhook and Telegram hook triggers (src/hook_triggers.py)."""

import json
import os
import unittest
from unittest.mock import patch

from src import agent_api, hook_triggers
from src.engine import all_workflows, matches


class StubTable:
    def __init__(self, items=None):
        self.items = {item["hook_id"]: dict(item) for item in (items or [])}

    def scan(self, Limit=200):
        return {"Items": [dict(value) for value in self.items.values()]}

    def get_item(self, Key):
        item = self.items.get(Key["hook_id"])
        return {"Item": dict(item)} if item else {}

    def put_item(self, Item):
        self.items[Item["hook_id"]] = dict(Item)

    def delete_item(self, Key):
        self.items.pop(Key["hook_id"], None)


class StubConnections:
    def __init__(self, items):
        self.items = {item["connection_id"]: dict(item) for item in items}

    def get_item(self, Key):
        item = self.items.get(Key["connection_id"])
        return {"Item": dict(item)} if item else {}


def telegram_connection(connection_id="tg-bot"):
    return {
        "connection_id": connection_id,
        "provider": "telegram",
        "status": "connected",
        "credential_id": f"oauth#{connection_id}",
    }


WEBHOOK_BODY = {"name": "orders", "actions": [{"type": "webhook", "url": "https://hooks.test/x"}]}


class WebhookSaveTests(unittest.TestCase):
    def test_save_generates_token_and_url(self):
        with patch.dict(os.environ, {"HOOK_TRIGGERS_TABLE": "hooks", "HOOKS_BASE_URL": "https://dapier.example.test"}):
            status, payload = hook_triggers.api_save(dict(WEBHOOK_BODY), "op", table_ref=StubTable())
        self.assertEqual(status, 200)
        self.assertTrue(payload["created"])
        self.assertEqual(payload["url"], "https://dapier.example.test/hooks/webhook/orders")
        self.assertGreaterEqual(len(payload["token"]), 32)
        self.assertEqual(payload["header"], "authorization")
        self.assertEqual(payload["auth_scheme"], "Bearer")

    def test_update_keeps_token_unless_rotated(self):
        stub = StubTable()
        with patch.dict(os.environ, {"HOOK_TRIGGERS_TABLE": "hooks", "HOOKS_BASE_URL": "https://dapier.example.test"}):
            _status, created = hook_triggers.api_save(dict(WEBHOOK_BODY), "op", table_ref=stub)
            _status, updated = hook_triggers.api_save(
                dict(WEBHOOK_BODY, description="later"), "op", table_ref=stub)
            _status, rotated = hook_triggers.api_save(
                dict(WEBHOOK_BODY, rotate_token=True), "op", table_ref=stub)
        self.assertFalse(updated["created"])
        self.assertEqual(updated["token"], created["token"])
        self.assertNotEqual(rotated["token"], created["token"])

    def test_rejects_bad_names_and_empty_actions(self):
        with patch.dict(os.environ, {"HOOK_TRIGGERS_TABLE": "hooks"}):
            with self.assertRaises(hook_triggers.TriggerError):
                hook_triggers.api_save({"name": "nope!", "actions": WEBHOOK_BODY["actions"]},
                                       "op", table_ref=StubTable())
            with self.assertRaises(hook_triggers.TriggerError):
                hook_triggers.api_save({"name": "ok-name", "actions": []}, "op", table_ref=StubTable())

    def test_telegram_send_action_is_allowed(self):
        with patch.dict(os.environ, {"HOOK_TRIGGERS_TABLE": "hooks"}):
            item, _created = hook_triggers.build_item(
                {"name": "orders", "kind": "webhook",
                 "actions": [{"type": "telegram_send", "connection_id": "tg-bot"}]},
                "op", "webhook")
        self.assertEqual(item["actions"][0]["type"], "telegram_send")

    def test_kind_cannot_hijack_an_existing_name(self):
        stub = StubTable([{"hook_id": "orders", "kind": "telegram", "token": "t",
                           "connection_id": "tg-bot", "actions": [], "enabled": True}])
        with patch.dict(os.environ, {"HOOK_TRIGGERS_TABLE": "hooks"}):
            with self.assertRaises(hook_triggers.TriggerError) as ctx:
                hook_triggers.api_save(dict(WEBHOOK_BODY), "op", "webhook", table_ref=stub)
        self.assertIn("telegram trigger", str(ctx.exception))
        self.assertEqual(stub.items["orders"]["kind"], "telegram")


class TelegramSaveTests(unittest.TestCase):
    def setUp(self):
        self.env = patch.dict(os.environ, {
            "HOOK_TRIGGERS_TABLE": "hooks",
            "HOOKS_BASE_URL": "https://dapier.example.test",
        })
        self.env.start()
        self.addCleanup(self.env.stop)
        self.connections = StubConnections([telegram_connection()])
        credential_patch = patch("src.credentials.get_credential",
                                 return_value={"token": "bot-token"})
        credential_patch.start()
        self.addCleanup(credential_patch.stop)
        self.registered = []
        self.transport = lambda method, url, body=None, **kwargs: (
            self.registered.append((url, json.loads(body or b"{}"))) or (200, b'{"ok":true}'))

    def api_save(self, body, table=None):
        return hook_triggers.api_save(
            body, "op", "telegram", table_ref=table or StubTable(),
            connections_table=self.connections, transport=self.transport)

    def test_save_registers_telegram_webhook_with_secret(self):
        status, payload = self.api_save({
            "name": "bot-inbox",
            "connection_id": "tg-bot",
            "actions": [{"type": "telegram_send", "connection_id": "tg-bot"}],
        })
        self.assertEqual(status, 200)
        self.assertEqual(payload["url"], "https://dapier.example.test/hooks/telegram/bot-inbox")
        self.assertEqual(payload["connection_id"], "tg-bot")
        self.assertEqual(payload["header"], "x-telegram-bot-api-secret-token")
        self.assertEqual(len(self.registered), 1)
        api_url, request_body = self.registered[0]
        self.assertTrue(api_url.endswith("/setWebhook"))
        self.assertEqual(request_body["url"], "https://dapier.example.test/hooks/telegram/bot-inbox")
        self.assertEqual(request_body["secret_token"], payload["token"])

    def test_requires_a_connected_telegram_connection(self):
        with self.assertRaises(hook_triggers.TriggerError) as ctx:
            self.api_save({"name": "bot-inbox", "actions": WEBHOOK_BODY["actions"]})
        self.assertIn("connection_id", str(ctx.exception))
        with self.assertRaises(hook_triggers.TriggerError):
            self.api_save({"name": "bot-inbox", "connection_id": "dropbox",
                           "actions": WEBHOOK_BODY["actions"]})
        self.connections = StubConnections([dict(telegram_connection(), status="ready")])
        with self.assertRaises(hook_triggers.TriggerError):
            self.api_save({"name": "bot-inbox", "connection_id": "tg-bot",
                           "actions": WEBHOOK_BODY["actions"]})

    def test_one_webhook_per_bot(self):
        stub = StubTable()
        self.api_save({"name": "first", "connection_id": "tg-bot",
                       "actions": WEBHOOK_BODY["actions"]}, table=stub)
        self.registered.clear()
        with self.assertRaises(hook_triggers.TriggerError) as ctx:
            self.api_save({"name": "second", "connection_id": "tg-bot",
                           "actions": WEBHOOK_BODY["actions"]}, table=stub)
        self.assertIn("one webhook per bot", str(ctx.exception))
        self.assertEqual(self.registered, [])

    def test_disable_releases_the_bot(self):
        stub = StubTable()
        self.api_save({"name": "first", "connection_id": "tg-bot",
                       "actions": WEBHOOK_BODY["actions"]}, table=stub)
        self.api_save({"name": "first", "connection_id": "tg-bot", "enabled": False,
                       "actions": WEBHOOK_BODY["actions"]}, table=stub)
        # A second trigger can now claim the bot.
        self.registered.clear()
        status, _payload = self.api_save({"name": "second", "connection_id": "tg-bot",
                                          "actions": WEBHOOK_BODY["actions"]},
                                         table=StubTable())
        self.assertEqual(status, 200)
        self.assertEqual(len(self.registered), 1)


class ListAndDeleteTests(unittest.TestCase):
    def setUp(self):
        self.env = patch.dict(os.environ, {"HOOK_TRIGGERS_TABLE": "hooks"})
        self.env.start()
        self.addCleanup(self.env.stop)

    def test_list_filters_by_kind(self):
        stub = StubTable([
            {"hook_id": "orders", "kind": "webhook", "url": "u1", "token": "t1",
             "actions": [], "enabled": True},
            {"hook_id": "bot-inbox", "kind": "telegram", "url": "u2", "token": "t2",
             "connection_id": "tg-bot", "actions": [], "enabled": True},
        ])
        status, payload = hook_triggers.api_list(stub)
        self.assertEqual(status, 200)
        self.assertEqual([h["hook_id"] for h in payload["hooks"]], ["bot-inbox", "orders"])
        _status, only_telegram = hook_triggers.api_list(stub, kind="telegram")
        self.assertEqual([h["hook_id"] for h in only_telegram["hooks"]], ["bot-inbox"])

    def test_delete_missing_is_404_and_kind_mismatch_is_rejected(self):
        stub = StubTable([{"hook_id": "orders", "kind": "webhook", "actions": [], "enabled": True}])
        with self.assertRaises(hook_triggers.TriggerError):
            hook_triggers.api_delete("nope", "op", table_ref=stub)
        with self.assertRaises(hook_triggers.TriggerError):
            hook_triggers.api_delete("orders", "op", kind="telegram", table_ref=stub)
        status, payload = hook_triggers.api_delete("orders", "op", table_ref=stub)
        self.assertEqual(status, 200)
        self.assertEqual(stub.items, {})


class WorkflowMergeTests(unittest.TestCase):
    def test_webhook_workflow_matches_its_own_delivery_and_no_other(self):
        item = {"hook_id": "orders", "kind": "webhook", "url": "u", "token": "t",
                "actions": [{"type": "webhook", "url": "https://hooks.test/x"}], "enabled": True}
        workflow = hook_triggers.load_workflows(table_ref=StubTable([dict(item)]))[0]
        self.assertEqual(workflow["id"], "webhook-trigger-orders")
        self.assertTrue(matches(workflow, {
            "connector": "webhook", "event": "request.received", "data": {"hook": "orders"}}))
        self.assertFalse(matches(workflow, {
            "connector": "webhook", "event": "request.received", "data": {"hook": "other"}}))
        self.assertFalse(matches(workflow, {
            "connector": "telegram", "event": "request.received", "data": {"hook": "orders"}}))

    def test_all_workflows_appends_hook_triggers_to_yaml(self):
        stub = StubTable([{
            "hook_id": "orders", "kind": "webhook", "url": "u", "token": "t",
            "actions": [{"type": "webhook", "url": "https://hooks.test/x"}], "enabled": True,
        }])
        with patch.dict(os.environ, {"HOOK_TRIGGERS_TABLE": "hooks"}), \
             patch("src.hook_triggers.get_table", return_value=stub):
            merged = all_workflows()
        self.assertEqual(merged[-1]["id"], "webhook-trigger-orders")

    def test_disabled_triggers_do_not_run(self):
        stub = StubTable([{"hook_id": "orders", "kind": "webhook", "enabled": False, "actions": []}])
        self.assertEqual(hook_triggers.load_workflows(table_ref=stub), [])


class AgentApiTests(unittest.TestCase):
    def test_operator_can_create_and_list_hooks_over_bearer_auth(self):
        stub = StubTable()
        event = {"headers": {}, "body": json.dumps(dict(WEBHOOK_BODY))}
        with patch("src.agent_api.authenticate", return_value=("sub-1", None)), \
             patch("src.agent_api.authz.is_operator", return_value=True), \
             patch("src.hook_triggers.get_table", return_value=stub), \
             patch.dict(os.environ, {"HOOK_TRIGGERS_TABLE": "hooks",
                                     "CONNECTIONS_TABLE": "connections",
                                     "GRANTS_TABLE": "grants"}):
            created = agent_api.hook_triggers_api(event, "PUT")
            listed = agent_api.hook_triggers_api({"headers": {}}, "GET")
        self.assertEqual(created["statusCode"], 200)
        self.assertTrue(json.loads(created["body"])["created"])
        self.assertEqual(
            [h["hook_id"] for h in json.loads(listed["body"])["hooks"]], ["orders"])

    def test_non_operator_is_rejected(self):
        with patch("src.agent_api.require_operator",
                   return_value=(None, {"statusCode": 403})):
            response = agent_api.hook_triggers_api({"headers": {}}, "GET")
        self.assertEqual(response["statusCode"], 403)

    def test_bad_kind_is_400(self):
        with patch("src.agent_api.require_operator", return_value=("sub-1", None)), \
             patch.dict(os.environ, {"HOOK_TRIGGERS_TABLE": "hooks"}):
            response = agent_api.hook_triggers_api(
                {"headers": {}, "queryStringParameters": {"kind": "pigeon"}}, "GET")
        self.assertEqual(response["statusCode"], 400)


if __name__ == "__main__":
    unittest.main()
