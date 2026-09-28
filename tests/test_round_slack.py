"""Round-10 slack lane: distinct trigger events, the slack.messages poll
source, and the update-message / add-reaction actions.

Unit tests drive the new pieces with a fake provider transport and the
connection/token seams patched (the test_action_breadth pattern), asserting
method, URL, request body and the output shape. The sample half checks the
per-event sample chain (one documented delivery per declared event) and the
poll half the watermark contract: the first fire seeds without emitting, a
bot post never fires, and only strictly-newer messages publish.
"""
import json
from unittest.mock import patch

import pytest

from src.dapier.connectors import trigger_discovery
from src.dapier.connectors.registry import CONNECTORS
from src.dapier.triggers import poll_sources, poll_triggers
from src.dapier.triggers.intake import slack_events
from src.dapier.engine.actions.slack import (
    run_slack_add_reaction,
    run_slack_update_message,
)

import src.dapier.connectors  # noqa: F401  (import = registration)


SLACK_CONNECTION = {"connection_id": "slack-conn", "provider": "slack",
                    "status": "connected", "credential_id": "oauth#slack-conn"}

EVENT = {"connector": "slack", "event": "message.received",
         "data": {"channel_id": "C01BQC114P2", "user_id": "U02PFU1LS",
                  "text": "ship it", "ts": "1758900012.000300"}}


class FakeTransport:
    """Route provider calls by URL substring to canned JSON responses."""

    def __init__(self, *routes):
        self.routes = routes  # (substring, status, payload) tuples
        self.calls = []

    def __call__(self, method, url, *, headers=None, body=None, timeout=15):
        self.calls.append({"method": method, "url": url, "headers": headers,
                           "body": body})
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


# --- the four events, end to end -------------------------------------------------


def test_chip_declares_exactly_the_intake_events():
    assert CONNECTORS["slack"].events == slack_events.EVENTS
    assert slack_events.EVENTS == ("message.received", "app.mention",
                                   "reaction.added", "member.joined")


def test_intake_publishes_one_distinct_event_per_slack_type():
    assert slack_events.event_name("message.groups") == "message.received"
    assert slack_events.event_name("message") == "message.received"
    assert slack_events.event_name("app_mention") == "app.mention"
    assert slack_events.event_name("reaction_added") == "reaction.added"
    assert slack_events.event_name("member_joined_channel") == "member.joined"
    assert slack_events.event_name("pin_added") is None


def test_reaction_and_join_envelopes_carry_their_channel():
    reaction = slack_events.event_data({
        "event": {"type": "reaction_added", "user": "U1", "reaction": "tada",
                  "item": {"type": "message", "channel": "C1",
                           "ts": "1758900012.000300"},
                  "event_ts": "1758900100.000500"},
    }, "slack-conn")
    assert reaction["channel_id"] == "C1"
    assert reaction["reaction"] == "tada"
    assert reaction["item_ts"] == "1758900012.000300"
    assert reaction["ts"] == "1758900100.000500"
    joined = slack_events.event_data({
        "event": {"type": "member_joined_channel", "user": "U1",
                  "channel": "C2", "inviter": "U0"},
    }, "slack-conn")
    assert joined["channel_id"] == "C2" and joined["inviter"] == "U0"


@pytest.fixture(autouse=True)
def no_recorded_history(monkeypatch):
    """Pin the sample fallback chain to synthetic: history needs run tables."""
    monkeypatch.setattr(trigger_discovery, "history_sample",
                        lambda connector, event=None: None)


def _sample(**kwargs):
    status, payload = trigger_discovery.api_discover({"connector": "slack", **kwargs})
    assert status == 200, payload
    return payload


def test_every_declared_event_serves_its_own_sample(monkeypatch):
    def missing(*args, **kwargs):
        raise trigger_discovery.DiscoveryNotFound("no connection")

    monkeypatch.setattr(trigger_discovery, "connected_connection", missing)
    samples = {}
    for event in CONNECTORS["slack"].events:
        payload = _sample(event=event)
        assert payload["sample"]["event"] == event, event
        samples[event] = payload["sample"]["data"]
    assert samples["message.received"]["text"]
    assert samples["app.mention"]["text"].startswith("<@")
    assert samples["reaction.added"]["reaction"] == "tada"
    assert samples["reaction.added"]["channel_id"]
    assert samples["member.joined"]["inviter"]
    unknown = _sample(event="pin.added")
    assert unknown["sample"]["event"] == "message.received"


# --- the slack.messages poll source -----------------------------------------------


def test_poll_source_resolves_and_validates(monkeypatch):
    assert "slack.messages" in poll_sources.source_names()
    spec = poll_sources.resolve("slack.messages")
    assert spec.connector == "slack" and spec.event == "message.received"
    with pytest.raises(Exception) as excinfo:
        spec.validate({})
    assert "connection_id" in str(excinfo.value)
    with pytest.raises(Exception) as excinfo:
        spec.validate({"connection_id": "slack-conn"})
    assert "channel_id" in str(excinfo.value)
    extras = spec.validate({"connection_id": "slack-conn", "channel_id": " C1 "})
    assert extras["channel_id"] == "C1"
    assert extras["cursor_mode"] == "next_cursor" and extras["url"] == ""


def _history_transport(messages, next_cursor=""):
    return FakeTransport(("conversations.history", 200, {
        "ok": True, "messages": messages,
        "response_metadata": {"next_cursor": next_cursor},
    }))


HISTORY = [
    {"type": "message", "user": "U1", "text": "newest", "ts": "1758900100.000500"},
    {"type": "message", "bot_id": "B0T", "text": "loop?", "ts": "1758900090.000400"},
    {"type": "message", "subtype": "channel_join", "user": "U2",
     "text": "U2 has joined", "ts": "1758900080.000300"},
    {"type": "message", "user": "U3", "text": "older", "ts": "1758900012.000200"},
]


def test_poll_first_fire_seeds_without_emitting(monkeypatch):
    configure_connections(monkeypatch, [SLACK_CONNECTION],
                          credentials=lambda key: {"token": "xoxb-tok"})
    transport = _history_transport(HISTORY)
    item = {"poll_id": "p", "connection_id": "slack-conn", "channel_id": "C1"}
    items, cursor = poll_sources.resolve("slack.messages").fetch(item, None,
                                                                 transport=transport)
    assert items == [] and cursor == "1758900100.000500"
    call = transport.calls[0]
    assert call["method"] == "POST"
    assert call["url"] == "https://slack.com/api/conversations.history"
    assert json.loads(call["body"]) == {"channel": "C1", "limit": 200}
    assert call["headers"]["authorization"] == "Bearer xoxb-tok"


def test_poll_emits_only_messages_newer_than_the_cursor(monkeypatch):
    configure_connections(monkeypatch, [SLACK_CONNECTION],
                          credentials=lambda key: {"token": "xoxb-tok"})
    transport = _history_transport(HISTORY)
    item = {"poll_id": "p", "connection_id": "slack-conn", "channel_id": "C1"}
    items, cursor = poll_sources.resolve("slack.messages").fetch(
        item, "1758900012.000200", transport=transport)
    # The bot post and the membership notice never fire, like the intake.
    assert [entry["id"] for entry in items] == ["1758900100.000500"]
    assert cursor == "1758900100.000500"
    newest = items[0]
    assert newest["channel_id"] == "C1" and newest["text"] == "newest"
    assert newest["type"] == "message" and newest["user_id"] == "U1"


def test_poll_build_item_and_event_shape(monkeypatch):
    item = poll_triggers.build_item({
        "name": "slack-feed", "source": "slack.messages",
        "expression": "rate(5 minutes)", "connection_id": "slack-conn",
        "channel_id": "C1",
        "actions": [{"type": "webhook", "url": "https://hooks.test/x"}],
    }, "op")
    assert item["source"] == "slack.messages" and item["channel_id"] == "C1"
    configure_connections(monkeypatch, [SLACK_CONNECTION],
                          credentials=lambda key: {"token": "xoxb-tok"})
    transport = _history_transport(HISTORY)
    raw = {"id": "1758900100.000500", "type": "message", "channel_id": "C1",
           "user_id": "U1", "text": "newest", "ts": "1758900100.000500"}
    event = poll_triggers.event_for(item, raw)
    assert event["connector"] == "slack" and event["event"] == "message.received"
    assert event["data"]["poll"] == "slack-feed"
    assert event["data"]["item_id"] == "1758900100.000500"


def test_poll_fetch_failure_is_a_runtime_error(monkeypatch):
    configure_connections(monkeypatch, [SLACK_CONNECTION],
                          credentials=lambda key: {"token": "xoxb-tok"})
    transport = FakeTransport(("conversations.history", 200, {"ok": False, "error": "channel_not_found"}))
    item = {"poll_id": "p", "connection_id": "slack-conn", "channel_id": "C1"}
    with pytest.raises(RuntimeError) as excinfo:
        poll_sources.resolve("slack.messages").fetch(item, None, transport=transport)
    assert "slack poll failed" in str(excinfo.value)


def test_poll_fetch_needs_its_connection_and_channel():
    with pytest.raises(RuntimeError) as excinfo:
        poll_sources.resolve("slack.messages").fetch({"poll_id": "p"}, None)
    assert "connection_id" in str(excinfo.value)
    with pytest.raises(RuntimeError) as excinfo:
        poll_sources.resolve("slack.messages").fetch(
            {"poll_id": "p", "connection_id": "slack-conn"}, None)
    assert "channel_id" in str(excinfo.value)


# --- the update / react actions ----------------------------------------------------


def test_update_message_posts_chat_update():
    transport = FakeTransport(("chat.update", 200, {
        "ok": True, "channel": "C01BQC114P2", "ts": "1758900012.000300",
        "text": "ship it (edited)"}))
    output = run_slack_update_message(
        {"type": "slack_update_message", "connection_id": "slack-conn",
         "channel": "{channel_id}", "ts": "{ts}", "text": "{text} (edited)"},
        EVENT, transport=transport)
    assert output == {"ok": True, "channel": "C01BQC114P2",
                      "ts": "1758900012.000300"}
    call = transport.calls[0]
    assert call["url"] == "https://slack.com/api/chat.update"
    assert call["headers"]["authorization"] == "Bearer xoxb-tok"
    assert json.loads(call["body"]) == {"channel": "C01BQC114P2",
                                        "ts": "1758900012.000300",
                                        "text": "ship it (edited)"}


def test_update_message_provider_error_raises():
    transport = FakeTransport(("chat.update", 200,
                               {"ok": False, "error": "message_not_found"}))
    with pytest.raises(RuntimeError) as excinfo:
        run_slack_update_message(
            {"type": "slack_update_message", "connection_id": "slack-conn",
             "channel": "C1", "ts": "1.1", "text": "x"},
            EVENT, transport=transport)
    assert "message_not_found" in str(excinfo.value)


def test_add_reaction_posts_reactions_add():
    transport = FakeTransport(("reactions.add", 200, {"ok": True}))
    output = run_slack_add_reaction(
        {"type": "slack_add_reaction", "connection_id": "slack-conn",
         "channel": "{channel_id}", "timestamp": "{ts}", "reaction": "tada"},
        EVENT, transport=transport)
    assert output == {"ok": True, "reaction": "tada", "channel": "C01BQC114P2",
                      "ts": "1758900012.000300"}
    call = transport.calls[0]
    assert call["url"] == "https://slack.com/api/reactions.add"
    assert json.loads(call["body"]) == {"channel": "C01BQC114P2",
                                        "timestamp": "1758900012.000300",
                                        "name": "tada"}


def test_add_reaction_treats_already_reacted_as_success():
    transport = FakeTransport(("reactions.add", 200,
                               {"ok": False, "error": "already_reacted"}))
    output = run_slack_add_reaction(
        {"type": "slack_add_reaction", "connection_id": "slack-conn",
         "channel": "C1", "timestamp": "1.1", "reaction": ":tada:"},
        EVENT, transport=transport)
    assert output == {"ok": True, "reaction": "tada", "channel": "C1",
                      "ts": "1.1", "already_reacted": True}


def test_add_reaction_requires_renderable_fields():
    transport = FakeTransport()
    with pytest.raises(ValueError):
        run_slack_add_reaction(
            {"type": "slack_add_reaction", "connection_id": "slack-conn",
             "channel": "C1", "timestamp": "", "reaction": "tada"},
            EVENT, transport=transport)


@pytest.fixture(autouse=True)
def stored_token(monkeypatch):
    """Every slack action/poll run resolves the connection's stored token."""
    configure_connections(monkeypatch, [SLACK_CONNECTION],
                          credentials=lambda key: {"token": "xoxb-tok"})
