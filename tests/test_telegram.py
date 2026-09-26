"""Tests for the Telegram Bot API helper and the telegram_send action."""

import json
import unittest
from unittest.mock import patch

from src.dapier.connections.providers import telegram_api
from src.dapier.engine import execute, run_telegram_send
from src.dapier.engine.actions import slack as slack_action


def stub_transport(result, status=200, *, capture=None):
    def transport(method, url, *, headers=None, body=None, timeout=10):
        if capture is not None:
            capture.append({"method": method, "url": url,
                            "body": json.loads(body or b"{}"), "headers": headers})
        return status, json.dumps({"ok": True, "result": result}).encode()
    return transport


class TokenValidationTests(unittest.TestCase):
    def test_accepts_botfather_shape(self):
        token = "123456789:AAH9qN4Q7Efh3example_token_value123"
        self.assertEqual(telegram_api.validate_token(f" {token} "), token)

    def test_rejects_bad_shapes(self):
        for token in ("", "xoxb-123", "123456:short", "no-colon-here-valuevalue"):
            with self.assertRaises(telegram_api.TelegramApiError):
                telegram_api.validate_token(token)


class ApiCallTests(unittest.TestCase):
    def test_get_me_returns_bot_identity(self):
        calls = []
        transport = stub_transport({"id": 42, "username": "dapier_bot"}, capture=calls)
        bot_id, title = telegram_api.get_me("123:tokenvaluetokenvaluevaluevalu", transport=transport)
        self.assertEqual(bot_id, 42)
        self.assertEqual(title, "@dapier_bot")
        self.assertIn("bot123:", calls[0]["url"])
        self.assertTrue(calls[0]["url"].endswith("/getMe"))

    def test_not_ok_result_fails_closed(self):
        def transport(method, url, **kwargs):
            return 200, b'{"ok": false, "description": "Unauthorized"}'
        with self.assertRaises(telegram_api.TelegramApiError) as ctx:
            telegram_api.get_me("123:tokenvaluetokenvaluevaluevalu", transport=transport)
        self.assertIn("Unauthorized", str(ctx.exception))
        with self.assertRaises(telegram_api.TelegramApiError):
            telegram_api.get_me("123:tokenvaluetokenvaluevaluevalu",
                                transport=lambda *a, **k: (_ for _ in ()).throw(OSError()))

    def test_set_webhook_sends_url_and_secret(self):
        calls = []
        transport = stub_transport(True, capture=calls)
        telegram_api.set_webhook("123:tokenvaluetokenvaluevaluevalu",
                                 "https://dapier.example.test/hooks/telegram/orders",
                                 "secret-value", transport=transport)
        self.assertEqual(calls[0]["body"]["url"], "https://dapier.example.test/hooks/telegram/orders")
        self.assertEqual(calls[0]["body"]["secret_token"], "secret-value")


class SendMessageActionTests(unittest.TestCase):
    CONNECTION = {
        "connection_id": "tg-bot",
        "provider": "telegram",
        "status": "connected",
        "credential_id": "oauth#tg-bot",
    }

    def run_action(self, event, action=None, token="bot-token"):
        calls = []
        transport = stub_transport({"message_id": 7}, capture=calls)
        with patch("src.dapier.engine.actions.base._connected_connection", return_value=dict(self.CONNECTION)), \
             patch("src.dapier.connections.credentials.get_credential", return_value={"token": token}):
            run_telegram_send(action or {"type": "telegram_send", "connection_id": "tg-bot",
                                         "chat_id": "101"},
                              event, transport=transport)
        return calls

    def test_sends_text_to_the_named_chat(self):
        calls = self.run_action({"connector": "email", "event": "message.received",
                                 "data": {"subject": "invoice"}})
        self.assertEqual(calls[0]["body"]["chat_id"], "101")
        self.assertEqual(calls[0]["body"]["text"], "")  # default template {text}; data has no text

    def test_template_formats_event_fields(self):
        calls = self.run_action(
            {"data": {"subject": "invoice", "route": "todo"}},
            action={"type": "telegram_send", "connection_id": "tg-bot", "chat_id": 101,
                    "text": "New mail: {subject} ({route})"})
        self.assertEqual(calls[0]["body"]["text"], "New mail: invoice (todo)")

    def test_chat_id_falls_back_to_the_triggering_chat(self):
        calls = self.run_action({"connector": "telegram", "event": "message.received",
                                 "data": {"chat_id": 555, "text": "hi"}},
                                action={"type": "telegram_send", "connection_id": "tg-bot"})
        self.assertEqual(calls[0]["body"]["chat_id"], 555)
        self.assertEqual(calls[0]["body"]["text"], "hi")

    def test_missing_chat_id_is_an_error(self):
        with patch("src.dapier.engine.actions.base._connected_connection", return_value=dict(self.CONNECTION)), \
             patch("src.dapier.connections.credentials.get_credential", return_value={"token": "t"}):
            with self.assertRaises(ValueError):
                run_telegram_send({"type": "telegram_send", "connection_id": "tg-bot"},
                                  {"data": {}},
                                  transport=stub_transport(True))

    def test_execute_dispatches_telegram_send(self):
        event = {"connector": "telegram", "event": "message.received",
                 "data": {"hook": "bot-inbox", "chat_id": 555, "text": "ping"}}
        recorded = []

        def fake_send(token, chat_id, text, *, transport=None, timeout=10):
            recorded.append({"token": token, "chat_id": chat_id, "text": text})
            return {"message_id": 1}

        item = {"hook_id": "bot-inbox", "kind": "telegram", "url": "u", "token": "t",
                "actions": [{"type": "telegram_send", "connection_id": "tg-bot"}],
                "enabled": True}
        stub = type("T", (), {
            "scan": lambda self, Limit=200: {"Items": [dict(item)]},
            "get_item": lambda self, Key: {},
            "put_item": lambda self, Item: None,
            "delete_item": lambda self, Key: None,
        })()
        with patch.dict("os.environ", {"HOOK_TRIGGERS_TABLE": "hooks"}), \
             patch("src.dapier.triggers.hook_triggers.get_table", return_value=stub), \
             patch("src.dapier.engine.actions.base._connected_connection", return_value=dict(self.CONNECTION)), \
             patch("src.dapier.connections.credentials.get_credential", return_value={"token": "bot-token"}), \
             patch("src.dapier.connections.providers.telegram_api.send_message", side_effect=fake_send):
            execute(event)
        self.assertEqual(recorded, [{"token": "bot-token", "chat_id": 555, "text": "ping"}])


class SlackTelegramFormatTests(unittest.TestCase):
    """The slack action's telegram_format mode (the au-tomator port)."""

    ACTION = {"type": "slack", "connection_id": "slack", "channel": "course",
              "telegram_format": True, "source_link": "https://t.me/c/{message_id}"}

    def run_slack(self, event, action=None):
        calls = []

        def fake_json_request(url, payload, headers=None, timeout=10):
            calls.append({"url": url, "payload": payload, "headers": headers})
            return {"ok": True, "ts": f"ts-{len(calls)}", "channel": "C1"}

        with patch.object(slack_action, "_token_for", return_value="xoxb-test"), \
             patch.object(slack_action.base, "_json_request", side_effect=fake_json_request):
            output = slack_action.run_slack(action or dict(self.ACTION), event)
        return calls, output

    def test_posts_blocks_then_the_source_link_in_the_thread(self):
        event = {"connector": "telegram", "event": "message.received",
                 "data": {"text": "hello", "entities": [], "message_id": 42}}
        calls, output = self.run_slack(event)

        self.assertEqual(calls[0]["payload"]["channel"], "course")
        self.assertEqual(calls[0]["payload"]["blocks"],
                         [{"type": "section", "text": {"type": "mrkdwn", "text": ">hello"}}])
        self.assertNotIn("thread_ts", calls[0]["payload"])
        self.assertEqual(calls[1]["payload"]["thread_ts"], "ts-1")
        self.assertEqual(calls[1]["payload"]["blocks"],
                         [{"type": "section", "text": {"type": "mrkdwn",
                                                       "text": "https://t.me/c/42"}}])
        self.assertEqual(output, {"ok": True, "channel": "course", "ts": "ts-1", "messages": 2})
        self.assertTrue(calls[0]["headers"]["authorization"].startswith("Bearer xoxb-"))

    def test_entities_render_as_mrkdwn_blocks(self):
        event = {"connector": "telegram", "event": "message.received",
                 "data": {"text": "go bold", "entities": [
                     {"offset": 3, "length": 4, "type": "bold"}], "message_id": 1}}
        calls, _ = self.run_slack(event)
        self.assertEqual(
            calls[0]["payload"]["blocks"][0]["text"]["text"], ">go *bold*")

    def test_long_post_continues_in_the_thread_before_the_link(self):
        text = "\n".join(f"line number {i}" for i in range(600))
        event = {"connector": "telegram", "event": "message.received",
                 "data": {"text": text, "entities": [], "message_id": 9}}
        calls, output = self.run_slack(event)

        groups = len(calls) - 1  # every message after the first is a thread reply
        self.assertGreater(groups, 1)
        for call in calls[1:]:
            self.assertEqual(call["payload"]["thread_ts"], "ts-1")
        self.assertEqual(output["messages"], len(calls))

    def test_media_post_falls_back_to_the_placeholder_text(self):
        event = {"connector": "telegram", "event": "message.received",
                 "data": {"text": "", "entities": [], "message_id": 5}}
        calls, _ = self.run_slack(event)
        self.assertEqual(
            calls[0]["payload"]["blocks"][0]["text"]["text"],
            ">no text in this post, see Telegram for the attachment")

    def test_slack_rejection_fails_the_action(self):
        event = {"connector": "telegram", "event": "message.received",
                 "data": {"text": "hello", "entities": [], "message_id": 1}}
        with patch.object(slack_action, "_token_for", return_value="xoxb-test"), \
             patch.object(slack_action.base, "_json_request",
                          return_value={"ok": False, "error": "not_in_channel"}):
            with self.assertRaises(RuntimeError) as ctx:
                slack_action.run_slack(dict(self.ACTION), event)
        self.assertIn("not_in_channel", str(ctx.exception))


class TelegramToSlackFlowTests(unittest.TestCase):
    """The bundled telegram-to-slack flow, end to end through the engine."""

    TRIGGER = {"hook_id": "automator-telegram", "kind": "telegram", "url": "u",
               "token": "t", "flow": "telegram-to-slack", "actions": [],
               "connection_id": "tg-bot", "enabled": True}

    def stub_tables(self):
        trigger = dict(self.TRIGGER)
        table = type("T", (), {
            "scan": lambda self, Limit=200: {"Items": [dict(trigger)]},
            "get_item": lambda self, Key: {"Item": dict(trigger)},
            "put_item": lambda self, Item: None,
            "delete_item": lambda self, Key: None,
        })()
        return table

    def run_event(self, data):
        calls = []

        def fake_json_request(url, payload, headers=None, timeout=10):
            calls.append(payload)
            return {"ok": True, "ts": f"ts-{len(calls)}", "channel": "C"}

        with patch.dict("os.environ", {"HOOK_TRIGGERS_TABLE": "hooks"}), \
             patch("src.dapier.triggers.hook_triggers.get_table", return_value=self.stub_tables()), \
             patch.object(slack_action, "_token_for", return_value="xoxb-test"), \
             patch.object(slack_action.base, "_json_request", side_effect=fake_json_request):
            execute({"connector": "telegram", "event": "message.received", "data": data})
        return calls

    def test_flow_is_in_the_bundled_catalog(self):
        from src.dapier.engine import flow_catalog

        names = [flow["name"] for flow in flow_catalog()]
        self.assertIn("telegram-to-slack", names)

    def test_channel_post_routes_to_its_slack_channel(self):
        calls = self.run_event({"hook": "automator-telegram", "chat_id": -1001730331343,
                                "message_id": 7, "text": "new cohort starts",
                                "entities": [], "is_channel_post": True})
        self.assertEqual(calls[0]["channel"], "course-mlops-zoomcamp")
        self.assertEqual(calls[0]["blocks"],
                         [{"type": "section", "text": {"type": "mrkdwn",
                                                       "text": ">new cohort starts"}}])
        self.assertEqual(calls[1]["blocks"][0]["text"]["text"],
                         "https://t.me/dtc_courses/7")

    def test_unknown_chat_posts_nothing(self):
        calls = self.run_event({"hook": "automator-telegram", "chat_id": -42,
                                "message_id": 7, "text": "stranger", "entities": []})
        self.assertEqual(calls, [])

    def test_string_chat_id_filters_match_numeric_updates(self):
        calls = self.run_event({"hook": "automator-telegram", "chat_id": "-1002136268305",
                                "message_id": 3, "text": "llm news", "entities": []})
        self.assertEqual(calls[0]["channel"], "course-llm-zoomcamp")


if __name__ == "__main__":
    unittest.main()
