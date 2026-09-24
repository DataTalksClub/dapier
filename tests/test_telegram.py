"""Tests for the Telegram Bot API helper and the telegram_send action."""

import json
import unittest
from unittest.mock import patch

from src.dapier.connections.providers import telegram_api
from src.dapier.engine import execute, run_telegram_send


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


if __name__ == "__main__":
    unittest.main()
