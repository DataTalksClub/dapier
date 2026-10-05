"""Tests for webhook, Telegram, and Mailchimp hook triggers."""

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from src.dapier.api import agent as agent_api
from src.dapier.triggers import hook_triggers


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


WEBHOOK_BODY = {"name": "orders"}


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

    def test_mailchimp_kind_reserves_the_mailchimp_url(self):
        body = dict(WEBHOOK_BODY, kind="mailchimp", list_id="abc123")
        credential = patch("src.dapier.connections.credentials.get_credential",
                           return_value={"apiKey": "abc123-us12"})
        transport = lambda method, url, body=None, **kwargs: (200, b'{"id": "wh1"}')
        credential.start()
        self.addCleanup(credential.stop)
        with patch.dict(os.environ, {"HOOK_TRIGGERS_TABLE": "hooks", "HOOKS_BASE_URL": "https://dapier.example.test"}):
            status, payload = hook_triggers.api_save(body, "op", "mailchimp", table_ref=StubTable(),
                                                     transport=transport)
        self.assertEqual(status, 200)
        self.assertEqual(payload["url"], "https://dapier.example.test/hooks/mailchimp/orders")
        self.assertEqual(payload["kind"], "mailchimp")
        self.assertEqual(payload["list_id"], "abc123")
        self.assertNotIn("warnings", payload)

    def test_mailchimp_kind_accepts_webhook_response_semantics(self):
        body = dict(WEBHOOK_BODY, kind="mailchimp", list_id="abc123",
                    response={"mode": "ack"}, dedupe_path="data.email")
        credential = patch("src.dapier.connections.credentials.get_credential",
                           return_value={"apiKey": "abc123-us12"})
        transport = lambda method, url, body=None, **kwargs: (200, b'{"id": "wh1"}')
        credential.start()
        self.addCleanup(credential.stop)
        with patch.dict(os.environ, {"HOOK_TRIGGERS_TABLE": "hooks", "HOOKS_BASE_URL": "https://dapier.example.test"}):
            status, payload = hook_triggers.api_save(body, "op", "mailchimp", table_ref=StubTable(),
                                                     transport=transport)
        self.assertEqual(status, 200)
        self.assertEqual(payload["dedupe_path"], "data.email")
        self.assertNotIn("warnings", payload)

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

    def test_rejects_bad_names(self):
        with patch.dict(os.environ, {"HOOK_TRIGGERS_TABLE": "hooks"}):
            with self.assertRaises(hook_triggers.TriggerError):
                hook_triggers.api_save({"name": "nope!"}, "op", table_ref=StubTable())

    def test_kind_cannot_hijack_an_existing_name(self):
        stub = StubTable([{"hook_id": "orders", "kind": "telegram", "token": "t",
                           "connection_id": "tg-bot", "enabled": True}])
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
        credential_patch = patch("src.dapier.connections.credentials.get_credential",
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
            self.api_save({"name": "bot-inbox"})
        self.assertIn("connection_id", str(ctx.exception))
        with self.assertRaises(hook_triggers.TriggerError):
            self.api_save({"name": "bot-inbox", "connection_id": "dropbox"})
        self.connections = StubConnections([dict(telegram_connection(), status="ready")])
        with self.assertRaises(hook_triggers.TriggerError):
            self.api_save({"name": "bot-inbox", "connection_id": "tg-bot"})

    def test_one_webhook_per_bot(self):
        stub = StubTable()
        self.api_save({"name": "first", "connection_id": "tg-bot"}, table=stub)
        self.registered.clear()
        with self.assertRaises(hook_triggers.TriggerError) as ctx:
            self.api_save({"name": "second", "connection_id": "tg-bot"}, table=stub)
        self.assertIn("one webhook per bot", str(ctx.exception))
        self.assertEqual(self.registered, [])

    def test_disable_releases_the_bot(self):
        stub = StubTable()
        self.api_save({"name": "first", "connection_id": "tg-bot"}, table=stub)
        self.api_save({"name": "first", "connection_id": "tg-bot", "enabled": False},
                      table=stub)
        # A second trigger can now claim the bot.
        self.registered.clear()
        status, _payload = self.api_save({"name": "second", "connection_id": "tg-bot"},
                                         table=StubTable())
        self.assertEqual(status, 200)
        self.assertEqual(len(self.registered), 1)

    def test_telegram_event_for_names_channel_announcements(self):
        self.assertEqual(hook_triggers.telegram_event_for(
            {"update_id": 1, "message": {"text": "hi"}}), "message.received")
        self.assertEqual(hook_triggers.telegram_event_for(
            {"update_id": 2, "channel_post": {"text": "ann",
                                              "chat": {"id": -100, "type": "channel"}}}),
            "channel_post.received")
        self.assertEqual(hook_triggers.telegram_event_for(
            {"update_id": 3, "edited_channel_post": {"text": "edit",
                                                     "chat": {"id": -100}}}),
            "channel_post.received")
        self.assertEqual(hook_triggers.telegram_event_for(
            {"update_id": 4, "callback_query": {"id": "cq1", "data": "join",
                                                "from": {"id": 9}}}),
            "callback_query.received")
        self.assertEqual(hook_triggers.telegram_event_for(None), "message.received")

    def test_update_data_flattens_callback_queries(self):
        """A button tap flattens into what a workflow filters on: the tap's
        id and payload, the tapper, and the originating message's chat
        fields; the raw update rides along like the message events."""
        data = hook_triggers.update_data({
            "update_id": 95,
            "callback_query": {
                "id": "4382bfdwdsb323b2d9",
                "from": {"id": 9, "first_name": "Ada", "username": "ada"},
                "message": {"message_id": 27, "text": "Pick a cohort:",
                            "chat": {"id": 555, "type": "private"}},
                "chat_instance": "-9923423423",
                "data": "join:september",
            },
        }, "bot-inbox")
        self.assertEqual(data["hook"], "bot-inbox")
        self.assertEqual(data["update_id"], 95)
        self.assertEqual(data["id"], "4382bfdwdsb323b2d9")
        self.assertEqual(data["data"], "join:september")
        self.assertEqual(data["from"]["username"], "ada")
        self.assertEqual(data["message_id"], 27)
        self.assertEqual(data["text"], "Pick a cohort:")
        self.assertEqual(data["chat_id"], 555)
        self.assertEqual(data["inline_message_id"], None)
        self.assertEqual(data["update"]["callback_query"]["chat_instance"], "-9923423423")

    def test_update_data_flattens_inline_callback_queries(self):
        """A tap on a keyboard of a message sent via inline mode carries an
        inline_message_id and no message — the flatten keeps it addressable."""
        data = hook_triggers.update_data({
            "update_id": 96,
            "callback_query": {
                "id": "cq-inline", "from": {"id": 9, "first_name": "Ada"},
                "inline_message_id": "BQAAAAIAAAABmQAAAOZpq78",
                "chat_instance": "-9923423423", "data": "vote:yes",
            },
        }, "bot-inbox")
        self.assertEqual(data["id"], "cq-inline")
        self.assertEqual(data["inline_message_id"], "BQAAAAIAAAABmQAAAOZpq78")
        self.assertEqual(data["message_id"], None)
        self.assertEqual(data["text"], "")
        self.assertEqual(data["chat_id"], None)


class MailchimpTransport:
    """Records every Marketing API call behind a mailchimp trigger's
    lifecycle; answers GET listings with ``webhooks`` and mutations with a
    created webhook id."""

    def __init__(self, webhooks=None, status=200):
        self.calls = []
        self.webhooks = list(webhooks or [])
        self.status = status

    def __call__(self, method, url, headers=None, body=None, timeout=None):
        self.calls.append((method, url, json.loads(body) if body else None))
        payload = {"webhooks": self.webhooks} if method == "GET" else {"id": "wh1"}
        return self.status, json.dumps(payload).encode()


class MailchimpTriggerTests(unittest.TestCase):
    BODY = {"name": "audience", "list_id": "abc123"}

    def setUp(self):
        self.env = patch.dict(os.environ, {
            "HOOK_TRIGGERS_TABLE": "hooks",
            "HOOKS_BASE_URL": "https://dapier.example.test",
        })
        self.env.start()
        self.addCleanup(self.env.stop)
        credential_patch = patch("src.dapier.connections.credentials.get_credential",
                                 return_value={"apiKey": "abc123-us12"})
        credential_patch.start()
        self.addCleanup(credential_patch.stop)
        self.transport = MailchimpTransport()

    def methods(self):
        return [(method, url) for method, url, _body in self.transport.calls]

    def api_save(self, body, table=None, transport=None):
        return hook_triggers.api_save(
            body, "op", "mailchimp", table_ref=table or StubTable(),
            transport=transport or self.transport)

    def test_save_registers_the_audience_webhook_with_subscribed_types(self):
        status, payload = self.api_save(dict(self.BODY, events=["subscribe"]))
        self.assertEqual(status, 200)
        self.assertNotIn("warnings", payload)
        # the audience is listed (nothing stale to remove on a fresh save),
        # then the callback URL is registered for exactly the subscribed types
        self.assertEqual(self.methods(), [
            ("GET", "https://us12.api.mailchimp.com/3.0/lists/abc123/webhooks"),
            ("POST", "https://us12.api.mailchimp.com/3.0/lists/abc123/webhooks"),
        ])
        _method, _url, registered = self.transport.calls[-1]
        self.assertEqual(registered["url"],
                         "https://dapier.example.test/hooks/mailchimp/audience")
        self.assertEqual(registered["events"], {
            name: name == "subscribe" for name in hook_triggers.MAILCHIMP_EVENT_TYPES})
        # no bearer hint: Mailchimp sends no auth headers
        self.assertNotIn("header", payload)
        self.assertNotIn("auth_scheme", payload)
        self.assertEqual(payload["list_id"], "abc123")

    def test_omitted_events_default_to_every_type(self):
        _status, payload = self.api_save(dict(self.BODY))
        self.assertEqual(payload["events"], list(hook_triggers.MAILCHIMP_EVENT_TYPES))
        _method, _url, registered = self.transport.calls[-1]
        self.assertTrue(all(registered["events"].values()))

    def test_reregistration_replaces_a_stale_webhook_on_the_same_url(self):
        self.transport.webhooks = [
            {"id": "wh-old", "url": "https://dapier.example.test/hooks/mailchimp/audience"},
            {"id": "wh-keep", "url": "https://example.test/someone-else"},
        ]
        status, payload = self.api_save(dict(self.BODY, events=["cleaned"]))
        self.assertEqual(status, 200)
        self.assertNotIn("warnings", payload)
        self.assertEqual(self.methods(), [
            ("GET", "https://us12.api.mailchimp.com/3.0/lists/abc123/webhooks"),
            ("DELETE", "https://us12.api.mailchimp.com/3.0/lists/abc123/webhooks/wh-old"),
            ("POST", "https://us12.api.mailchimp.com/3.0/lists/abc123/webhooks"),
        ])

    def test_disable_and_delete_unsubscribe_from_mailchimp(self):
        stub = StubTable()
        self.api_save(dict(self.BODY), table=stub)
        self.transport.webhooks = [
            {"id": "wh1", "url": "https://dapier.example.test/hooks/mailchimp/audience"}]
        self.transport.calls.clear()
        status, payload = self.api_save(dict(self.BODY, enabled=False), table=stub)
        self.assertEqual(status, 200)
        self.assertNotIn("warnings", payload)
        self.assertEqual(self.methods(), [
            ("GET", "https://us12.api.mailchimp.com/3.0/lists/abc123/webhooks"),
            ("DELETE", "https://us12.api.mailchimp.com/3.0/lists/abc123/webhooks/wh1"),
        ])

        self.transport.calls.clear()
        status, payload = hook_triggers.api_delete("audience", "op", table_ref=stub,
                                                   transport=self.transport)
        self.assertEqual(status, 200)
        self.assertTrue(payload["ok"])
        self.assertNotIn("warnings", payload)
        self.assertEqual([method for method, _url in self.methods()], ["GET", "DELETE"])
        self.assertEqual(stub.items, {})

    def test_a_mailchimp_outage_warns_but_the_save_lands(self):
        stub = StubTable()
        status, payload = self.api_save(dict(self.BODY), table=stub,
                                        transport=MailchimpTransport(status=500))
        self.assertEqual(status, 200)
        self.assertEqual(len(payload.get("warnings") or []), 1)
        self.assertIn("registration failed", payload["warnings"][0])
        self.assertIn("audience", stub.items)


class HubTransport:
    """Records every WebSub hub call behind a youtube trigger's save."""

    def __init__(self, status=200):
        self.calls = []
        self.status = status

    def __call__(self, method, url, headers=None, body=None, timeout=None):
        self.calls.append({"method": method, "url": url,
                           "body": body.decode() if body else None})
        return self.status, b""


class YouTubeSaveTests(unittest.TestCase):
    """Subscribe-on-save for youtube hook triggers: saving subscribes the
    watched channel on the WebSub hub (best-effort, mirroring Mailchimp's
    lifecycle), disabling/deleting never unsubscribes (the hub callback is
    shared by every watcher of the channel), and the stored trigger's
    workflow matches the hub's channel-scoped deliveries."""

    BODY = {"name": "uploads", "channel_id": "UCa"}

    def setUp(self):
        self.env = patch.dict(os.environ, {
            "HOOK_TRIGGERS_TABLE": "hooks",
            "HOOKS_BASE_URL": "https://dapier.example.test",
            "YOUTUBE_CALLBACK_URL": "https://dapier.example.test/hooks/youtube",
            "YOUTUBE_WEBHOOK_SECRET_ID": "secret-id",
        })
        self.env.start()
        self.addCleanup(self.env.stop)
        import src.dapier.triggers.intake.youtube_subscriptions as youtube_subscriptions

        fake_secrets = type("B", (), {"client": staticmethod(
            lambda name: type("S", (), {
                "get_secret_value": staticmethod(
                    lambda SecretId: {"SecretString": "hub-secret"})})())})
        secrets_patch = patch.object(youtube_subscriptions, "boto3", fake_secrets)
        secrets_patch.start()
        self.addCleanup(secrets_patch.stop)
        self.transport = HubTransport()

    def api_save(self, body, table=None, transport=None):
        return hook_triggers.api_save(
            body, "op", "youtube", table_ref=table or StubTable(),
            transport=transport or self.transport)

    def hub_bodies(self):
        import urllib.parse
        return [urllib.parse.parse_qs(call["body"]) for call in self.transport.calls]

    def test_save_subscribes_the_channel_on_the_hub(self):
        status, payload = self.api_save(dict(self.BODY))
        self.assertEqual(status, 200)
        self.assertNotIn("warnings", payload)
        self.assertEqual(payload["channel_id"], "UCa")
        self.assertEqual(payload["kind"], "youtube")
        # no auth headers: YouTube verifies the shared callback by signature
        self.assertNotIn("header", payload)
        self.assertNotIn("auth_scheme", payload)
        body = self.hub_bodies()[0]
        self.assertEqual(body["hub.mode"], ["subscribe"])
        self.assertEqual(body["hub.topic"],
                         ["https://www.youtube.com/feeds/videos.xml?channel_id=UCa"])
        self.assertEqual(body["hub.callback"],
                         ["https://dapier.example.test/hooks/youtube"])
        self.assertEqual(body["hub.secret"], ["hub-secret"])

    def test_a_hub_failure_warns_but_the_save_lands(self):
        stub = StubTable()
        status, payload = self.api_save(dict(self.BODY), table=stub,
                                        transport=HubTransport(status=500))
        self.assertEqual(status, 200)
        self.assertEqual(len(payload.get("warnings") or []), 1)
        self.assertIn("WebSub subscription failed", payload["warnings"][0])
        self.assertIn("uploads", stub.items)

    def test_resave_refreshes_and_disable_never_unsubscribes(self):
        stub = StubTable()
        self.api_save(dict(self.BODY), table=stub)
        self.api_save(dict(self.BODY), table=stub)  # idempotent re-subscribe
        self.assertEqual(len(self.transport.calls), 2)
        self.transport.calls.clear()
        status, payload = self.api_save(dict(self.BODY, enabled=False), table=stub)
        self.assertEqual(status, 200)
        self.assertNotIn("warnings", payload)
        self.assertEqual(self.transport.calls, [])
        status, payload = hook_triggers.api_delete("uploads", "op", kind="youtube",
                                                   table_ref=stub,
                                                   transport=self.transport)
        self.assertEqual(status, 200)
        self.assertEqual(self.transport.calls, [])
        self.assertEqual(stub.items, {})

    def test_channel_id_is_required_and_response_is_rejected(self):
        with patch.dict(os.environ, {"HOOK_TRIGGERS_TABLE": "hooks"}):
            with self.assertRaises(hook_triggers.TriggerError) as ctx:
                hook_triggers.build_item({"name": "uploads"}, "op", "youtube")
            self.assertIn("channel_id", str(ctx.exception))
            with self.assertRaises(hook_triggers.TriggerError):
                hook_triggers.build_item(
                    {"name": "uploads", "channel_id": "UCa",
                     "response": {"mode": "sync"}},
                    "op", "youtube")

    def test_renewal_schedule_picks_the_channel_up_from_the_hook(self):
        """Hooks are trigger records now — the renewal reads the hook store
        directly, so a youtube hook's subscription outlives its save."""
        from src.dapier.triggers.intake import youtube_subscriptions

        stub = StubTable()
        self.api_save(dict(self.BODY), table=stub)
        self.assertEqual(youtube_subscriptions.channels_from_hooks(table_ref=stub),
                         ["UCa"])
        with patch.dict(os.environ, {"HOOK_TRIGGERS_TABLE": "hooks"}), \
                patch("src.dapier.triggers.hook_triggers.get_table", return_value=stub):
            self.assertEqual(youtube_subscriptions.channels_from_hooks(), ["UCa"])


class ListAndDeleteTests(unittest.TestCase):
    def setUp(self):
        self.env = patch.dict(os.environ, {"HOOK_TRIGGERS_TABLE": "hooks"})
        self.env.start()
        self.addCleanup(self.env.stop)

    def test_list_filters_by_kind(self):
        stub = StubTable([
            {"hook_id": "orders", "kind": "webhook", "url": "u1", "token": "t1",
             "enabled": True},
            {"hook_id": "bot-inbox", "kind": "telegram", "url": "u2", "token": "t2",
             "connection_id": "tg-bot", "enabled": True},
        ])
        status, payload = hook_triggers.api_list(stub)
        self.assertEqual(status, 200)
        self.assertEqual([h["hook_id"] for h in payload["hooks"]], ["bot-inbox", "orders"])
        _status, only_telegram = hook_triggers.api_list(stub, kind="telegram")
        self.assertEqual([h["hook_id"] for h in only_telegram["hooks"]], ["bot-inbox"])

    def test_delete_missing_is_404_and_kind_mismatch_is_rejected(self):
        stub = StubTable([{"hook_id": "orders", "kind": "webhook", "enabled": True}])
        with self.assertRaises(hook_triggers.TriggerError):
            hook_triggers.api_delete("nope", "op", table_ref=stub)
        with self.assertRaises(hook_triggers.TriggerError):
            hook_triggers.api_delete("orders", "op", kind="telegram", table_ref=stub)
        status, payload = hook_triggers.api_delete("orders", "op", table_ref=stub)
        self.assertEqual(status, 200)
        self.assertEqual(stub.items, {})

    def test_delete_removes_the_hooks_retry_state(self):
        from src.dapier.triggers import seen

        class CursorTable:
            items = {"seen#hook#orders": {"seen": {"request-1": 123}}}

            def delete_item(self, Key):
                self.items.pop(Key["cursor_id"], None)

        cursors = CursorTable()
        hooks = StubTable([{"hook_id": "orders", "kind": "webhook"}])
        with patch.object(seen, "get_table", return_value=cursors):
            status, _ = hook_triggers.api_delete("orders", "op", table_ref=hooks)
        self.assertEqual(status, 200)
        self.assertEqual(cursors.items, {})
        self.assertEqual(hooks.items, {})


class AgentApiTests(unittest.TestCase):
    def test_operator_can_create_and_list_hooks_over_bearer_auth(self):
        stub = StubTable()
        event = {"headers": {}, "body": json.dumps(dict(WEBHOOK_BODY))}
        with patch("src.dapier.api.agent.authenticate", return_value=("sub-1", None)), \
             patch("src.dapier.api.agent.authz.is_operator", return_value=True), \
             patch("src.dapier.triggers.hook_triggers.get_table", return_value=stub), \
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
        with patch("src.dapier.api.agent.require_operator",
                   return_value=(None, {"statusCode": 403})):
            response = agent_api.hook_triggers_api({"headers": {}}, "GET")
        self.assertEqual(response["statusCode"], 403)

    def test_bad_kind_is_400(self):
        with patch("src.dapier.api.agent.require_operator", return_value=("sub-1", None)), \
             patch.dict(os.environ, {"HOOK_TRIGGERS_TABLE": "hooks"}):
            response = agent_api.hook_triggers_api(
                {"headers": {}, "queryStringParameters": {"kind": "pigeon"}}, "GET")
        self.assertEqual(response["statusCode"], 400)


if __name__ == "__main__":
    unittest.main()
