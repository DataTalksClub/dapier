"""Round-11 slack/telegram lane: the invite / pin / find-message actions on
Slack and the edit-message / pin-message / ban / unban actions on Telegram.

Unit tests drive the new runners with a fake provider transport and the
connection/token seams patched (the test_round_slack /
test_round_sheetstelegram pattern), asserting method, URL, request body and
the output shape — including the benign-error verdicts (Slack's
``already_in_channel``, Telegram's ``true`` results) and telegram_send's
chat_id fallback to the triggering chat. The registry half checks the new
types are registered with their field specs and discover markers, and that
a chain using them validates.
"""
import json

import pytest

from src.dapier.connectors import registry
from plugins.slack.runners.slack import (
    run_slack_find_message,
    run_slack_invite_to_channel,
    run_slack_pin_message,
)
from src.dapier.engine.actions.telegram import (
    run_telegram_ban_member,
    run_telegram_edit_message,
    run_telegram_pin_message,
    run_telegram_unban_member,
)

import src.dapier.connectors  # noqa: F401  (import = registration)


SLACK_CONNECTION = {"connection_id": "slack-conn", "provider": "slack",
                    "status": "connected", "credential_id": "oauth#slack-conn"}
TELEGRAM_CONNECTION = {"connection_id": "tg-bot", "provider": "telegram",
                       "status": "connected", "credential_id": "oauth#tg-bot"}
BOT_TOKEN = "123456:AAH9qN4Q7Efh3example_token_value123"

SLACK_EVENT = {"connector": "slack", "event": "message.received",
               "data": {"channel_id": "C01BQC114P2", "user_id": "U02PFU1LS",
                        "text": "ship it", "ts": "1758900012.000300"}}
TELEGRAM_EVENT = {"connector": "telegram", "event": "message.received",
                  "data": {"chat_id": 555, "message_id": 7, "user_id": 42,
                           "text": "ship it"}}


class FakeTransport:
    """Route provider calls by URL substring to canned JSON responses."""

    def __init__(self, *routes):
        self.routes = routes  # (substring, status, payload) tuples
        self.calls = []

    def __call__(self, method, url, *, headers=None, body=None, timeout=15):
        self.calls.append({"method": method, "url": url, "headers": headers,
                           "body": body, "timeout": timeout})
        for substring, status, payload in self.routes:
            if substring in url:
                return status, json.dumps(payload).encode()
        raise AssertionError(f"unexpected provider call: {method} {url}")


def configure_connections(monkeypatch, connections=(), credentials=None):
    """A fake connections store plus a stored-credential seam."""
    tables = {"connections": {item["connection_id"]: dict(item)
                              for item in connections}}

    class Dynamo:
        def Table(self, name):
            holder = tables[name]

            class T:
                def get_item(self, Key):
                    item = holder.get(Key["connection_id"])
                    return {"Item": dict(item)} if item else {}

            return T()

    import boto3

    monkeypatch.setenv("CONNECTIONS_TABLE", "connections")
    monkeypatch.setattr(boto3, "resource", lambda service: Dynamo())
    if credentials is not None:
        monkeypatch.setattr(
            "src.dapier.connections.credentials.get_credential", credentials)


def configure_slack(monkeypatch):
    """Every slack action run resolves the connection's stored token."""
    configure_connections(monkeypatch, [SLACK_CONNECTION],
                          credentials=lambda key: {"token": "xoxb-tok"})


def configure_telegram(monkeypatch):
    """Every telegram action run resolves the bot connection's stored token."""
    monkeypatch.setattr("src.dapier.engine.actions.base._connected_connection",
                        lambda connection_id: dict(TELEGRAM_CONNECTION))
    monkeypatch.setattr("src.dapier.connections.credentials.get_credential",
                        lambda key: {"token": BOT_TOKEN})


def ok(result):
    """A Telegram Bot API success envelope around ``result``."""
    return {"ok": True, "result": result}


# --- slack_invite_to_channel -------------------------------------------------------


def test_invite_posts_conversations_invite(monkeypatch):
    configure_slack(monkeypatch)
    transport = FakeTransport(("conversations.invite", 200, {"ok": True}))
    output = run_slack_invite_to_channel(
        {"type": "slack_invite_to_channel", "connection_id": "slack-conn",
         "channel": "{channel_id}", "users": "{user_id},U03"},
        SLACK_EVENT, transport=transport)
    assert output == {"invited": True, "channel": "C01BQC114P2",
                      "users": "U02PFU1LS,U03"}
    call = transport.calls[0]
    assert call["method"] == "POST"
    assert call["url"] == "https://slack.com/api/conversations.invite"
    assert call["headers"]["authorization"] == "Bearer xoxb-tok"
    assert json.loads(call["body"]) == {"channel": "C01BQC114P2",
                                        "users": "U02PFU1LS,U03"}


def test_invite_absorbs_already_in_channel(monkeypatch):
    configure_slack(monkeypatch)
    transport = FakeTransport(("conversations.invite", 200,
                               {"ok": False, "error": "already_in_channel"}))
    output = run_slack_invite_to_channel(
        {"type": "slack_invite_to_channel", "connection_id": "slack-conn",
         "channel": "C01BQC114P2", "users": "U02PFU1LS"},
        SLACK_EVENT, transport=transport)
    assert output == {"invited": False, "channel": "C01BQC114P2",
                      "users": "U02PFU1LS", "reason": "already_in_channel"}


def test_invite_other_slack_errors_raise(monkeypatch):
    configure_slack(monkeypatch)
    transport = FakeTransport(("conversations.invite", 200,
                               {"ok": False, "error": "not_in_channel"}))
    with pytest.raises(RuntimeError) as excinfo:
        run_slack_invite_to_channel(
            {"type": "slack_invite_to_channel", "connection_id": "slack-conn",
             "channel": "C1", "users": "U1"},
            SLACK_EVENT, transport=transport)
    assert "not_in_channel" in str(excinfo.value)


def test_invite_requires_channel_and_users(monkeypatch):
    configure_slack(monkeypatch)
    transport = FakeTransport()
    with pytest.raises(ValueError):
        run_slack_invite_to_channel(
            {"type": "slack_invite_to_channel", "connection_id": "slack-conn",
             "channel": "C1", "users": " "},
            SLACK_EVENT, transport=transport)
    assert transport.calls == []


# --- slack_pin_message --------------------------------------------------------------


def test_pin_posts_pins_add(monkeypatch):
    configure_slack(monkeypatch)
    transport = FakeTransport(("pins.add", 200, {"ok": True}))
    output = run_slack_pin_message(
        {"type": "slack_pin_message", "connection_id": "slack-conn",
         "channel": "{channel_id}", "timestamp": "{ts}"},
        SLACK_EVENT, transport=transport)
    assert output == {"pinned": True, "channel": "C01BQC114P2",
                      "timestamp": "1758900012.000300"}
    call = transport.calls[0]
    assert call["url"] == "https://slack.com/api/pins.add"
    assert json.loads(call["body"]) == {"channel": "C01BQC114P2",
                                        "timestamp": "1758900012.000300"}


def test_pin_error_raises(monkeypatch):
    configure_slack(monkeypatch)
    transport = FakeTransport(("pins.add", 200, {"ok": False, "error": "no_item"}))
    with pytest.raises(RuntimeError) as excinfo:
        run_slack_pin_message(
            {"type": "slack_pin_message", "connection_id": "slack-conn",
             "channel": "C1", "timestamp": "1.1"},
            SLACK_EVENT, transport=transport)
    assert "no_item" in str(excinfo.value)


def test_pin_requires_channel_and_timestamp(monkeypatch):
    configure_slack(monkeypatch)
    transport = FakeTransport()
    with pytest.raises(ValueError):
        run_slack_pin_message(
            {"type": "slack_pin_message", "connection_id": "slack-conn",
             "channel": "C1", "timestamp": ""},
            SLACK_EVENT, transport=transport)
    assert transport.calls == []


# --- slack_find_message ---------------------------------------------------------------


def test_find_message_searches_and_shapes_the_matches(monkeypatch):
    configure_slack(monkeypatch)
    transport = FakeTransport(("search.messages", 200, {
        "ok": True,
        "messages": {"matches": [
            {"ts": "1758900012.000300", "channel": {"id": "C1", "name": "alerts"},
             "user": "U1", "text": "deploy done",
             "permalink": "https://slack.com/archives/C1/p1758900012000300"},
            "junk-not-a-dict"],
        }}))
    output = run_slack_find_message(
        {"type": "slack_find_message", "connection_id": "slack-conn",
         "query": "{text}", "count": "5"},
        SLACK_EVENT, transport=transport)
    assert output["found"] is True
    assert output["count"] == 1
    assert output["messages"] == [{
        "ts": "1758900012.000300", "channel_id": "C1", "channel_name": "alerts",
        "user": "U1", "text": "deploy done",
        "permalink": "https://slack.com/archives/C1/p1758900012000300"}]
    call = transport.calls[0]
    assert call["url"] == "https://slack.com/api/search.messages"
    assert json.loads(call["body"]) == {"query": "ship it", "count": 5}


def test_find_message_miss_is_a_verdict_not_an_error(monkeypatch):
    configure_slack(monkeypatch)
    transport = FakeTransport(("search.messages", 200,
                               {"ok": True, "messages": {"matches": []}}))
    output = run_slack_find_message(
        {"type": "slack_find_message", "connection_id": "slack-conn",
         "query": "nothing matches this"},
        SLACK_EVENT, transport=transport)
    assert output == {"found": False, "messages": [], "count": 0}
    # count is optional: the default page size applies.
    assert json.loads(transport.calls[0]["body"])["count"] == 20


def test_find_message_slack_errors_raise(monkeypatch):
    configure_slack(monkeypatch)
    transport = FakeTransport(("search.messages", 200,
                               {"ok": False, "error": "missing_scope"}))
    with pytest.raises(RuntimeError) as excinfo:
        run_slack_find_message(
            {"type": "slack_find_message", "connection_id": "slack-conn",
             "query": "deploy"},
            SLACK_EVENT, transport=transport)
    assert "missing_scope" in str(excinfo.value)


def test_find_message_requires_a_query(monkeypatch):
    configure_slack(monkeypatch)
    transport = FakeTransport()
    with pytest.raises(ValueError):
        run_slack_find_message(
            {"type": "slack_find_message", "connection_id": "slack-conn",
             "query": "  "},
            SLACK_EVENT, transport=transport)
    assert transport.calls == []


# --- telegram_edit_message -------------------------------------------------------------


def test_edit_message_edits_the_rendered_text(monkeypatch):
    configure_telegram(monkeypatch)
    transport = FakeTransport(("editMessageText", 200,
                               ok({"message_id": 8, "chat": {"id": 555}})))
    output = run_telegram_edit_message(
        {"type": "telegram_edit_message", "connection_id": "tg-bot",
         "chat_id": "555", "message_id": "{message_id}",
         "text": "{text} (edited)"},
        TELEGRAM_EVENT, transport=transport)
    assert output == {"message_id": 8, "chat_id": 555}
    call = transport.calls[0]
    assert call["url"] == f"https://api.telegram.org/bot{BOT_TOKEN}/editMessageText"
    assert json.loads(call["body"]) == {"chat_id": "555", "message_id": 7,
                                        "text": "ship it (edited)"}


def test_edit_message_chat_id_falls_back_to_the_triggering_chat(monkeypatch):
    configure_telegram(monkeypatch)
    transport = FakeTransport(("editMessageText", 200,
                               ok({"message_id": 8, "chat": {"id": 777}})))
    output = run_telegram_edit_message(
        {"type": "telegram_edit_message", "connection_id": "tg-bot",
         "message_id": "{message_id}", "text": "edited in place"},
        {"connector": "telegram", "event": "message.received",
         "data": {"chat_id": 777, "message_id": 3}},
        transport=transport)
    assert output == {"message_id": 8, "chat_id": 777}
    assert json.loads(transport.calls[0]["body"])["chat_id"] == 777


def test_edit_message_requires_a_message_id(monkeypatch):
    configure_telegram(monkeypatch)
    transport = FakeTransport()
    with pytest.raises(ValueError):
        run_telegram_edit_message(
            {"type": "telegram_edit_message", "connection_id": "tg-bot",
             "chat_id": "555", "message_id": " ", "text": "x"},
            TELEGRAM_EVENT, transport=transport)
    assert transport.calls == []


# --- telegram_ban_member / telegram_unban_member ----------------------------------------


def test_ban_member_bans_until_the_rendered_date(monkeypatch):
    configure_telegram(monkeypatch)
    transport = FakeTransport(("banChatMember", 200, ok(True)))
    output = run_telegram_ban_member(
        {"type": "telegram_ban_member", "connection_id": "tg-bot",
         "chat_id": "{chat_id}", "user_id": "{user_id}",
         "until_date": "1798761600"},
        TELEGRAM_EVENT, transport=transport)
    assert output == {"banned": True, "chat_id": "555", "user_id": 42}
    call = transport.calls[0]
    assert call["url"] == f"https://api.telegram.org/bot{BOT_TOKEN}/banChatMember"
    assert json.loads(call["body"]) == {"chat_id": "555", "user_id": 42,
                                        "until_date": 1798761600}


def test_unban_member_unbans(monkeypatch):
    configure_telegram(monkeypatch)
    transport = FakeTransport(("unbanChatMember", 200, ok(True)))
    output = run_telegram_unban_member(
        {"type": "telegram_unban_member", "connection_id": "tg-bot",
         "user_id": "{user_id}"},
        TELEGRAM_EVENT, transport=transport)
    assert output == {"unbanned": True, "chat_id": 555, "user_id": 42}
    call = transport.calls[0]
    assert call["url"] == f"https://api.telegram.org/bot{BOT_TOKEN}/unbanChatMember"
    assert json.loads(call["body"]) == {"chat_id": 555, "user_id": 42}


def test_member_actions_need_a_user_id(monkeypatch):
    configure_telegram(monkeypatch)
    transport = FakeTransport()
    for runner, type_ in ((run_telegram_ban_member, "telegram_ban_member"),
                          (run_telegram_unban_member, "telegram_unban_member")):
        with pytest.raises(ValueError):
            runner({"type": type_, "connection_id": "tg-bot",
                    "chat_id": "555", "user_id": ""},
                   TELEGRAM_EVENT, transport=transport)
    assert transport.calls == []


# --- telegram_pin_message ---------------------------------------------------------------


def test_pin_message_pins_silently_when_asked(monkeypatch):
    configure_telegram(monkeypatch)
    transport = FakeTransport(("pinChatMessage", 200, ok(True)))
    output = run_telegram_pin_message(
        {"type": "telegram_pin_message", "connection_id": "tg-bot",
         "chat_id": "{chat_id}", "message_id": "{message_id}",
         "disable_notification": "true"},
        TELEGRAM_EVENT, transport=transport)
    assert output == {"pinned": True, "chat_id": "555", "message_id": 7}
    call = transport.calls[0]
    assert call["url"] == f"https://api.telegram.org/bot{BOT_TOKEN}/pinChatMessage"
    assert json.loads(call["body"]) == {"chat_id": "555", "message_id": 7,
                                        "disable_notification": True}


def test_pin_message_requires_a_message_id(monkeypatch):
    configure_telegram(monkeypatch)
    transport = FakeTransport()
    with pytest.raises(ValueError):
        run_telegram_pin_message(
            {"type": "telegram_pin_message", "connection_id": "tg-bot",
             "chat_id": "555", "message_id": ""},
            TELEGRAM_EVENT, transport=transport)
    assert transport.calls == []


# --- registry wiring --------------------------------------------------------------------


ROUND_ACTION_SPECS = {
    "slack_invite_to_channel": ({"channel", "users"},
                                {"connection_id", "credential_id"}),
    "slack_pin_message": ({"channel", "timestamp"},
                          {"connection_id", "credential_id"}),
    "slack_find_message": ({"query"},
                           {"count", "connection_id", "credential_id"}),
    "telegram_edit_message": ({"connection_id", "message_id", "text"},
                              {"chat_id", "timeout_seconds"}),
    "telegram_pin_message": ({"connection_id", "message_id"},
                             {"chat_id", "disable_notification", "timeout_seconds"}),
    "telegram_ban_member": ({"connection_id", "user_id"},
                            {"chat_id", "until_date"}),
    "telegram_unban_member": ({"connection_id", "user_id"}, {"chat_id"}),
}


def test_round_actions_are_registered_with_their_specs():
    specs = registry.action_specs()
    for action_type, (required, optional) in ROUND_ACTION_SPECS.items():
        assert specs[action_type] == (frozenset(required), frozenset(optional)), action_type
    catalog = {entry["type"]: entry for entry in registry.catalog()["actions"]}
    for action_type in ROUND_ACTION_SPECS:
        assert action_type in catalog, action_type


def test_round_fields_carry_their_discover_markers():
    users_field = next(field for field
                       in registry.ACTIONS["slack_invite_to_channel"].fields
                       if field["key"] == "users")
    assert users_field.get("discover") == {"resource": "slack.users"}
    ts_field = next(field for field
                    in registry.ACTIONS["slack_pin_message"].fields
                    if field["key"] == "timestamp")
    assert ts_field.get("discover") == {"resource": "slack.messages",
                                        "params": {"channel": "channel"}}
    chat_field = next(field for field
                      in registry.ACTIONS["telegram_edit_message"].fields
                      if field["key"] == "chat_id")
    assert chat_field.get("discover") == {"resource": "telegram.chats"}
    bucket_field = next(field for field
                        in registry.ACTIONS["render_html_to_pdf"].fields
                        if field["key"] == "output_bucket")
    assert bucket_field.get("discover") == {"resource": "s3.buckets"}


def test_a_chain_using_the_round_actions_validates():
    registry.validate_action_chain([
        {"type": "slack_find_message", "connection_id": "slack-conn",
         "query": "{text}"},
        {"type": "slack_invite_to_channel", "connection_id": "slack-conn",
         "channel": "{channel_id}",
         "users": "{steps.find.output.user.id}"},
        {"type": "slack_pin_message", "connection_id": "slack-conn",
         "channel": "{channel_id}", "timestamp": "{ts}"},
        {"type": "telegram_edit_message", "connection_id": "tg-bot",
         "message_id": "{message_id}", "text": "{text} (edited)"},
        {"type": "telegram_pin_message", "connection_id": "tg-bot",
         "message_id": "{message_id}"},
        {"type": "telegram_ban_member", "connection_id": "tg-bot",
         "user_id": "{user_id}", "until_date": "1798761600"},
        {"type": "telegram_unban_member", "connection_id": "tg-bot",
         "user_id": "{user_id}"},
    ])


def test_validation_rejects_missing_and_unknown_keys():
    with pytest.raises(registry.ActionError) as excinfo:
        registry.validate_action_chain([
            {"type": "slack_invite_to_channel", "connection_id": "slack-conn",
             "channel": "C1"}])
    assert "missing: users" in str(excinfo.value)
    with pytest.raises(registry.ActionError) as excinfo:
        registry.validate_action_chain([
            {"type": "telegram_ban_member", "connection_id": "tg-bot",
             "user_id": "42", "forever": "true"}])
    assert "unknown keys: forever" in str(excinfo.value)
