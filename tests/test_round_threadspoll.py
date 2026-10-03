"""Action-staple tests for the threads/poll round: slack thread_ts replies,
slack_schedule_message (chat.scheduleMessage), telegram_send_poll (Bot API
sendPoll).

Unit tests drive each registered runner with the network faked — run_slack
sits on base._json_request, so that seam is patched (the test_engine.py
pattern); the schedule and poll runners take an injectable transport — and
the connection/token seams patched (the test_slack_actions /
test_round_sheetstelegram pattern), asserting method, URL, request body and
the step-output shape. The registry half checks the new types' field specs
and that validate_action_chain accepts a valid chain and rejects unknown
keys.
"""
import json
import unittest
from datetime import datetime, timezone
from unittest.mock import patch

from src.dapier.connectors import registry
from src.dapier.connections.providers import telegram_api
from plugins.slack.runners.slack import (
    run_slack,
    run_slack_schedule_message,
)
from src.dapier.engine.actions.telegram import run_telegram_send_poll

import src.dapier.connectors  # noqa: F401  (import = registration)


SLACK_TOKEN = "xoxb-test"
TELEGRAM_CONNECTION = {"connection_id": "tg-bot", "provider": "telegram",
                       "status": "connected", "credential_id": "oauth#tg-bot"}
BOT_TOKEN = "123456:AAH9qN4Q7Efh3example_token_value123"

SLACK_EVENT = {"connector": "slack", "event": "message.received",
               "data": {"channel_id": "C1", "ts": "111.222", "text": "ship it"}}
TELEGRAM_EVENT = {"connector": "telegram", "event": "message.received",
                  "data": {"chat_id": 555, "text": "hello"}}


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


class RecordingJsonRequest:
    """The base._json_request seam: records (url, payload), answers canned."""

    def __init__(self, response):
        self.response = response
        self.calls = []

    def __call__(self, url, payload, headers=None, timeout=10):
        self.calls.append({"url": url, "payload": payload,
                           "headers": headers, "timeout": timeout})
        return self.response


def json_body(payload):
    return json.dumps(payload).encode()


# --- slack thread_ts ------------------------------------------------------------


def run_post(json_request, action, event=None):
    action = {"type": "slack", "credential_id": "slack", "channel": "C1", **action}
    with patch("src.dapier.connections.credentials.get_credential",
               return_value={"token": SLACK_TOKEN}), \
         patch("src.dapier.engine.actions.base._json_request",
               side_effect=json_request):
        return run_slack(action, event or SLACK_EVENT)


class SlackThreadTsTests(unittest.TestCase):
    def test_post_omits_thread_ts_by_default(self):
        seam = RecordingJsonRequest({"ok": True, "channel": "C1", "ts": "111.333"})

        output = run_post(seam, {"text": "hello"})

        self.assertEqual(output, {"ok": True, "channel": "C1", "ts": "111.333"})
        call, = seam.calls
        self.assertEqual(call["url"], "https://slack.com/api/chat.postMessage")
        self.assertEqual(call["payload"],
                         {"channel": "C1", "text": "hello",
                          "unfurl_links": True, "unfurl_media": True})
        self.assertEqual(call["headers"]["authorization"], f"Bearer {SLACK_TOKEN}")

    def test_thread_ts_takes_a_template_and_replies_in_thread(self):
        seam = RecordingJsonRequest({"ok": True, "channel": "C1", "ts": "444.555"})

        output = run_post(seam, {"text": "a reply", "thread_ts": "{ts}"})

        self.assertTrue(output["ok"])
        payload = seam.calls[0]["payload"]
        self.assertEqual(payload["thread_ts"], "111.222")

    def test_blank_thread_ts_stays_absent(self):
        seam = RecordingJsonRequest({"ok": True, "channel": "C1", "ts": "1.2"})

        run_post(seam, {"text": "hello", "thread_ts": "  "})

        self.assertNotIn("thread_ts", seam.calls[0]["payload"])

    def test_slack_rejection_raises_with_the_code(self):
        seam = RecordingJsonRequest({"ok": False, "error": "is_archived"})

        with self.assertRaisesRegex(RuntimeError, "is_archived"):
            run_post(seam, {"text": "hello", "thread_ts": "{ts}"})


# --- slack_schedule_message -----------------------------------------------------


def run_schedule(transport, action, event=None, steps=None):
    action = {"type": "slack_schedule_message", "credential_id": "slack",
              "channel": "{channel_id}", "text": "{text}", **action}
    with patch("src.dapier.connections.credentials.get_credential",
               return_value={"token": SLACK_TOKEN}):
        return run_slack_schedule_message(action, event or SLACK_EVENT,
                                          steps=steps, transport=transport)


ISO_POST_AT = "2026-10-02T09:00:00Z"
ISO_POST_AT_EPOCH = int(datetime(2026, 10, 2, 9, 0, tzinfo=timezone.utc).timestamp())

SCHEDULED = {"ok": True, "channel": "C1", "scheduled_message_id": "Q125",
             "post_at": ISO_POST_AT_EPOCH, "ts": "150.001"}


class SlackScheduleMessageTests(unittest.TestCase):
    def test_iso_post_at_becomes_epoch_seconds(self):
        transport = FakeTransport(("chat.scheduleMessage", 200,
                                   json_body(SCHEDULED)))

        output = run_schedule(transport, {"post_at": ISO_POST_AT})

        self.assertEqual(output, {"ok": True, "channel": "C1",
                                  "scheduled_message_id": "Q125",
                                  "ts": "150.001",
                                  "post_at": ISO_POST_AT_EPOCH})
        call, = transport.calls
        self.assertEqual(call["method"], "POST")
        self.assertEqual(call["url"],
                         "https://slack.com/api/chat.scheduleMessage")
        self.assertEqual(call["headers"]["authorization"], f"Bearer {SLACK_TOKEN}")
        self.assertEqual(call["headers"]["content-type"], "application/json")
        payload = json.loads(call["body"])
        self.assertEqual(payload["channel"], "C1")
        self.assertEqual(payload["text"], "ship it")
        self.assertEqual(payload["post_at"], ISO_POST_AT_EPOCH)
        self.assertNotIn("thread_ts", payload)
        self.assertEqual(payload["unfurl_links"], True)
        self.assertEqual(payload["unfurl_media"], True)

    def test_offsetless_iso_and_date_only_read_as_utc(self):
        transport = FakeTransport(("chat.scheduleMessage", 200, json_body(SCHEDULED)))

        for raw in ("2026-10-02T09:00:00", "2026-10-02 09:00:00", "2026-10-02T09:00Z"):
            run_schedule(transport, {"post_at": raw})

        self.assertEqual(
            [json.loads(call["body"])["post_at"] for call in transport.calls[-3:]],
            [ISO_POST_AT_EPOCH] * 3)

    def test_epoch_seconds_pass_through(self):
        transport = FakeTransport(("chat.scheduleMessage", 200, json_body(SCHEDULED)))

        output = run_schedule(transport, {"post_at": " 1790000000 "})

        self.assertEqual(output["post_at"], 1790000000)
        self.assertEqual(json.loads(transport.calls[0]["body"])["post_at"],
                         1790000000)

    def test_post_at_takes_a_template(self):
        transport = FakeTransport(("chat.scheduleMessage", 200, json_body(SCHEDULED)))

        run_schedule(transport, {"post_at": "{steps.when.output.at}"},
                     steps={"when": {"output": {"at": ISO_POST_AT}}})

        self.assertEqual(json.loads(transport.calls[0]["body"])["post_at"],
                         ISO_POST_AT_EPOCH)

    def test_thread_ts_passes_through(self):
        transport = FakeTransport(("chat.scheduleMessage", 200, json_body(SCHEDULED)))

        output = run_schedule(transport, {"post_at": ISO_POST_AT,
                                          "thread_ts": "{ts}"})

        self.assertEqual(json.loads(transport.calls[0]["body"])["thread_ts"],
                         "111.222")
        self.assertTrue(output["ok"])

    def test_unfurl_flags_can_be_turned_off(self):
        transport = FakeTransport(("chat.scheduleMessage", 200, json_body(SCHEDULED)))

        run_schedule(transport, {"post_at": ISO_POST_AT,
                                 "unfurl_links": False, "unfurl_media": False})

        payload = json.loads(transport.calls[0]["body"])
        self.assertEqual(payload["unfurl_links"], False)
        self.assertEqual(payload["unfurl_media"], False)

    def test_requires_channel_text_and_post_at(self):
        transport = FakeTransport()

        for action, message in (
                ({"channel": "  ", "text": "hi", "post_at": ISO_POST_AT},
                 "requires a channel"),
                ({"channel": "C1", "text": "  ", "post_at": ISO_POST_AT},
                 "requires text"),
                ({"channel": "C1", "text": "hi"}, "requires post_at"),
                ({"channel": "C1", "text": "hi", "post_at": "  "},
                 "requires post_at")):
            with self.assertRaisesRegex(ValueError, message):
                run_schedule(transport, action)
        self.assertEqual(transport.calls, [])

    def test_unparseable_post_at_is_rejected_before_any_call(self):
        transport = FakeTransport()

        with self.assertRaisesRegex(ValueError, "ISO 8601 datetime or epoch"):
            run_schedule(transport, {"channel": "C1", "text": "hi",
                                     "post_at": "next tuesday"})
        self.assertEqual(transport.calls, [])

    def test_slack_error_raises_with_the_code(self):
        transport = FakeTransport(
            ("chat.scheduleMessage", 200,
             json_body({"ok": False, "error": "time_in_past"})))

        with self.assertRaisesRegex(RuntimeError, "time_in_past"):
            run_schedule(transport, {"post_at": ISO_POST_AT})

    def test_transport_outage_raises(self):
        transport = FakeTransport()

        with self.assertRaisesRegex(RuntimeError, "unreachable"):
            run_schedule(transport, {"post_at": ISO_POST_AT})


# --- telegram_send_poll ---------------------------------------------------------


def run_poll(transport, action, event=None, steps=None):
    action = {"type": "telegram_send_poll", "connection_id": "tg-bot", **action}
    with patch("src.dapier.engine.actions.base._connected_connection",
               return_value=dict(TELEGRAM_CONNECTION)), \
         patch("src.dapier.connections.credentials.get_credential",
               return_value={"token": BOT_TOKEN}):
        return run_telegram_send_poll(action, event or TELEGRAM_EVENT,
                                      steps=steps, transport=transport)


def poll_result(message_id=91, poll_id="883407088", question="Ship on Friday?"):
    return json_body({"ok": True, "result": {
        "message_id": message_id, "chat": {"id": 555},
        "poll": {"id": poll_id, "question": question}}})


class TelegramSendPollTests(unittest.TestCase):
    def test_posts_json_send_poll_and_returns_message_and_poll(self):
        transport = FakeTransport(("sendPoll", 200, poll_result()))

        output = run_poll(transport, {"question": "Ship on Friday?",
                                      "options": "Yes\nNo"})

        self.assertEqual(output, {
            "message_id": 91, "chat_id": 555,
            "poll": {"id": "883407088", "question": "Ship on Friday?"}})
        call, = transport.calls
        self.assertEqual(call["method"], "POST")
        self.assertEqual(call["url"],
                         f"https://api.telegram.org/bot{BOT_TOKEN}/sendPoll")
        self.assertEqual(call["headers"]["content-type"], "application/json")
        # no chat_id in the action: the triggering chat is answered
        self.assertEqual(json.loads(call["body"]), {
            "chat_id": 555, "question": "Ship on Friday?",
            "options": ["Yes", "No"], "is_anonymous": True})

    def test_options_split_on_newlines_and_trim_empties(self):
        transport = FakeTransport(("sendPoll", 200, poll_result(message_id=92)))

        run_poll(transport, {"question": "Lunch?",
                             "options": "  Pizza \n\n Sushi \n\n\n Tacos  "})

        payload = json.loads(transport.calls[0]["body"])
        self.assertEqual(payload["options"], ["Pizza", "Sushi", "Tacos"])

    def test_options_take_templates_per_line(self):
        transport = FakeTransport(("sendPoll", 200, poll_result(message_id=93)))

        run_poll(transport, {"question": "Deploy {subject}?",
                             "options": "{subject}\nHold"},
                  event={"data": {"chat_id": 555, "subject": "v2"}})

        payload = json.loads(transport.calls[0]["body"])
        self.assertEqual(payload["options"], ["v2", "Hold"])
        self.assertEqual(payload["question"], "Deploy v2?")

    def test_explicit_chat_id_wins_and_takes_a_template(self):
        transport = FakeTransport(("sendPoll", 200, poll_result(message_id=94)))

        output = run_poll(transport, {"chat_id": "{steps.open.output.chat}",
                                      "question": "q", "options": "a\nb"},
                          steps={"open": {"output": {"chat": "-100999"}}})

        self.assertEqual(json.loads(transport.calls[0]["body"])["chat_id"],
                         "-100999")
        self.assertEqual(output["chat_id"], 555)  # from the API's chat echo

    def test_anonymous_false_reaches_is_anonymous(self):
        transport = FakeTransport(("sendPoll", 200, poll_result(message_id=95)))

        run_poll(transport, {"question": "q", "options": "a\nb",
                             "anonymous": False})
        run_poll(transport, {"question": "q", "options": "a\nb",
                             "anonymous": "false"})

        self.assertEqual(json.loads(transport.calls[0]["body"])["is_anonymous"],
                         False)
        self.assertEqual(json.loads(transport.calls[1]["body"])["is_anonymous"],
                         False)

    def test_anonymous_defaults_to_true(self):
        transport = FakeTransport(("sendPoll", 200, poll_result(message_id=96)))

        run_poll(transport, {"question": "q", "options": "a\nb", "anonymous": ""})

        self.assertEqual(json.loads(transport.calls[0]["body"])["is_anonymous"],
                         True)

    def test_fewer_than_two_options_is_rejected_before_any_call(self):
        transport = FakeTransport()

        for raw in ("Only one", "", "\n\n", "   "):
            with self.assertRaises(ValueError) as caught:
                run_poll(transport, {"question": "q", "options": raw})
            self.assertIn("2-10", str(caught.exception))
        self.assertEqual(transport.calls, [])

    def test_more_than_ten_options_is_rejected(self):
        transport = FakeTransport()

        raw = "\n".join(f"opt {n}" for n in range(11))
        with self.assertRaises(ValueError) as caught:
            run_poll(transport, {"question": "q", "options": raw})
        self.assertIn("2-10", str(caught.exception))
        self.assertEqual(transport.calls, [])

    def test_missing_chat_and_question_fail_before_any_call(self):
        transport = FakeTransport()

        with self.assertRaisesRegex(ValueError, "chat_id"):
            run_poll(transport, {"question": "q", "options": "a\nb"},
                     event={"data": {}})
        with self.assertRaisesRegex(ValueError, "question"):
            run_poll(transport, {"question": "  ", "options": "a\nb"})
        self.assertEqual(transport.calls, [])

    def test_telegram_rejection_names_the_description(self):
        transport = FakeTransport(
            ("sendPoll", 400,
             json_body({"ok": False, "description": "question text is empty"})))

        with self.assertRaises(telegram_api.TelegramApiError) as caught:
            run_poll(transport, {"question": "q", "options": "a\nb"})
        self.assertIn("question text is empty", str(caught.exception))


# --- registry wiring ------------------------------------------------------------


class RegistryTests(unittest.TestCase):
    def test_new_actions_are_registered_with_their_specs(self):
        specs = registry.action_specs()
        slack_required, slack_optional = specs["slack"]
        self.assertEqual(slack_required, frozenset({"channel"}))
        self.assertIn("thread_ts", slack_optional)
        self.assertEqual(specs["slack_schedule_message"],
                         ({"channel", "text", "post_at"},
                          {"connection_id", "credential_id", "thread_ts",
                           "unfurl_links", "unfurl_media"}))
        self.assertEqual(specs["telegram_send_poll"],
                         ({"connection_id", "question", "options"},
                          {"chat_id", "anonymous", "timeout_seconds"}))

    def test_fields_carry_help_and_types(self):
        def field(type_, key):
            return next(field for field in registry.ACTIONS[type_].fields
                        if field["key"] == key)

        self.assertIn("message.received", field("slack", "thread_ts")["help"])
        self.assertIn("empty", field("slack", "thread_ts")["help"])
        post_at = field("slack_schedule_message", "post_at")
        self.assertTrue(post_at["required"])
        self.assertIn("epoch", post_at["help"])
        options = field("telegram_send_poll", "options")
        self.assertEqual(options["type"], "textarea")
        self.assertTrue(options["required"])
        self.assertIn("2 to 10", options["help"])
        self.assertEqual(field("telegram_send_poll", "anonymous")["type"],
                         "boolean")
        self.assertEqual(field("telegram_send_poll", "chat_id")["discover"],
                         {"resource": "telegram.chats"})

    def test_a_chain_using_the_new_actions_validates(self):
        registry.validate_action_chain([
            {"type": "slack", "connection_id": "slack-main",
             "channel": "{channel_id}", "text": "re: {text}",
             "thread_ts": "{ts}"},
            {"type": "slack_schedule_message", "connection_id": "slack-main",
             "channel": "C1", "text": "standup in 5",
             "post_at": "2026-10-02T09:00:00Z", "thread_ts": "{ts}"},
            {"type": "telegram_send_poll", "connection_id": "tg-bot",
             "question": "Ship on Friday?", "options": "Yes\nNo",
             "anonymous": False},
        ])

    def test_validation_rejects_unknown_keys(self):
        with self.assertRaises(registry.ActionError) as caught:
            registry.validate_action_chain([
                {"type": "slack_schedule_message", "connection_id": "slack-main",
                 "channel": "C1", "text": "hi",
                 "post_at": "2026-10-02T09:00:00Z", "post_in": "5m"}])
        self.assertIn("unknown keys: post_in", str(caught.exception))
        with self.assertRaises(registry.ActionError) as caught:
            registry.validate_action_chain([
                {"type": "telegram_send_poll", "connection_id": "tg-bot",
                 "question": "q", "options": "Yes\nNo", "is_anonymous": False}])
        self.assertIn("unknown keys: is_anonymous", str(caught.exception))

    def test_engine_dispatch_runs_the_new_actions(self):
        transport = FakeTransport(
            ("chat.scheduleMessage", 200, json_body(SCHEDULED)),
            ("sendPoll", 200, poll_result(message_id=97, poll_id="p9",
                                          question="q")))
        with patch("src.dapier.connections.credentials.get_credential",
                   return_value={"token": SLACK_TOKEN}), \
             patch("src.dapier.engine.actions.base._connected_connection",
                   return_value=dict(TELEGRAM_CONNECTION)), \
             patch("src.dapier.engine.actions.base._default_transport", transport), \
             patch("src.dapier.connections.providers.telegram_api._default_transport",
                   transport):
            scheduled = registry.run_action(
                {"type": "slack_schedule_message", "credential_id": "slack",
                 "channel": "C1", "text": "standup in 5", "post_at": ISO_POST_AT},
                SLACK_EVENT, "wf-1")
            poll = registry.run_action(
                {"type": "telegram_send_poll", "connection_id": "tg-bot",
                 "chat_id": "555", "question": "q", "options": "Yes\nNo"},
                TELEGRAM_EVENT, "wf-1")

        self.assertEqual(scheduled["scheduled_message_id"], "Q125")
        self.assertEqual(poll["message_id"], 97)


if __name__ == "__main__":
    unittest.main()
