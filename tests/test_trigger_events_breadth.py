"""Trigger-event breadth: telegram channel posts, callback queries, zoom.

Three events on the way to "every connector fires Zapier-style events", each
covered on both of its surfaces: the intake that publishes the event
(api.router._telegram_hook, triggers.intake.zoom_webhooks) and the sample
pull that documents it (plugins.telegram, plugins.zoom), so a filter
or template copied from a pulled sample matches a real delivery. Channel
announcements are their own telegram event (channel_post.received) with the
same hook/filter matching as message.received, and inline-keyboard button
taps a third (callback_query.received); zoom registrations arrive as
meeting.registration_created with the registrant's submitted fields.
"""
import json

import pytest

from src.dapier.connectors import trigger_discovery
from src.dapier.triggers import hook_triggers

import src.dapier.connectors  # noqa: F401  (import = registration)


# --- telegram intake: channel posts publish their own event ---------------------


def _hook_stub(hook_id="bot-inbox", kind="telegram", token="tg-secret", enabled=True):
    item = {"hook_id": hook_id, "kind": kind, "url": f"u-{hook_id}", "token": token,
            "actions": [], "enabled": enabled}
    return type("T", (), {
        "scan": lambda self, Limit=200: {"Items": [dict(item)]},
        "get_item": lambda self, Key: {"Item": dict(item)} if Key["hook_id"] == hook_id else {},
        "put_item": lambda self, Item: None,
        "delete_item": lambda self, Key: None,
    })()


def _post_telegram(monkeypatch, update, hook="bot-inbox"):
    """POST one raw update to the telegram hook; returns the published envelope."""
    from src.dapier.api import router as ingress

    sent = []
    monkeypatch.setenv("EVENT_QUEUE_URL", "https://sqs.example.test/events")
    monkeypatch.setenv("HOOK_TRIGGERS_TABLE", "hooks")
    monkeypatch.setattr(ingress.queue, "send_message", lambda **kwargs: sent.append(kwargs))
    monkeypatch.setattr("src.dapier.triggers.hook_triggers.get_table",
                        lambda *a, **k: _hook_stub(hook_id=hook))
    response = ingress.handler({
        "requestContext": {"http": {"method": "POST", "path": f"/hooks/telegram/{hook}"}},
        "headers": {"x-telegram-bot-api-secret-token": "tg-secret"},
        "body": json.dumps(update),
    }, None)
    assert response["statusCode"] == 200, response
    assert sent, "the delivery was accepted but nothing was published"
    return json.loads(sent[0]["MessageBody"])


def test_telegram_channel_post_publishes_channel_post_received(monkeypatch):
    envelope = _post_telegram(monkeypatch, {
        "update_id": 92, "channel_post": {
            "message_id": 12, "text": "announcement",
            "chat": {"id": -1001730331343, "title": "Courses", "type": "channel"},
            "author_signature": "DataTalksClub",
        }})
    assert envelope["connector"] == "telegram"
    assert envelope["event"] == "channel_post.received"
    data = envelope["data"]
    assert data["is_channel_post"] is True
    assert data["text"] == "announcement"
    assert data["chat_id"] == -1001730331343
    assert data["update"]["channel_post"]["author_signature"] == "DataTalksClub"


def test_telegram_edited_channel_post_is_still_a_channel_event(monkeypatch):
    envelope = _post_telegram(monkeypatch, {
        "update_id": 93, "edited_channel_post": {
            "message_id": 12, "text": "edited announcement",
            "chat": {"id": -1001730331343, "type": "channel"},
        }})
    assert envelope["event"] == "channel_post.received"
    assert envelope["data"]["is_channel_post"] is True


def test_telegram_plain_message_keeps_message_received(monkeypatch):
    envelope = _post_telegram(monkeypatch, {
        "update_id": 94, "message": {
            "message_id": 7, "text": "hello dapier",
            "chat": {"id": 555, "type": "private"},
            "from": {"id": 9, "first_name": "Ada"},
        }})
    assert envelope["event"] == "message.received"
    assert envelope["data"]["is_channel_post"] is False


def test_telegram_callback_query_publishes_callback_query_received(monkeypatch):
    """A button tap is its own event, flattened around the tap: the button's
    payload, the tapper, and the message the button rode on."""
    envelope = _post_telegram(monkeypatch, {
        "update_id": 95, "callback_query": {
            "id": "4382bfdwdsb323b2d9",
            "from": {"id": 9, "is_bot": False, "first_name": "Ada",
                     "username": "ada", "language_code": "en"},
            "message": {
                "message_id": 27, "text": "Pick a cohort:",
                "chat": {"id": 555, "type": "private", "first_name": "Ada"},
                "reply_markup": {"inline_keyboard": [[
                    {"text": "September", "callback_data": "join:september"}]]},
            },
            "chat_instance": "-9923423423",
            "data": "join:september",
        }})
    assert envelope["connector"] == "telegram"
    assert envelope["event"] == "callback_query.received"
    data = envelope["data"]
    assert data["id"] == "4382bfdwdsb323b2d9"
    assert data["data"] == "join:september"
    assert data["from"]["username"] == "ada"
    assert data["message_id"] == 27
    assert data["text"] == "Pick a cohort:"
    assert data["chat_id"] == 555
    assert data["inline_message_id"] is None
    assert data["update"]["callback_query"]["chat_instance"] == "-9923423423"


def test_channel_post_matches_the_same_stored_trigger_filters(monkeypatch):
    """Same filter/matching behavior as message.received: one stored telegram
    trigger matches all three events (messages, channel announcements,
    callback queries), so a chat_id (or any other) filter written for
    messages applies unchanged to the other two."""
    from src.dapier.engine import matches

    workflow = hook_triggers.workflow_for({
        "hook_id": "bot-inbox", "kind": "telegram", "actions": []})
    # workflow_for owns the trigger specs: one per telegram event, same hook
    assert [t["event"] for t in workflow["triggers"]] == \
        ["message.received", "channel_post.received", "callback_query.received"]
    assert all(t["filters"]["hook"] == {"equals": "bot-inbox"}
               for t in workflow["triggers"])
    for event in ("message.received", "channel_post.received",
                  "callback_query.received"):
        assert matches(workflow, {"connector": "telegram", "event": event,
                                  "data": {"hook": "bot-inbox"}}), event
    # a workflow author's extra filter (chat_id) applies unchanged to all
    scoped = {"enabled": True, "actions": [], "triggers": [
        {**spec, "filters": {**spec["filters"],
                             "chat_id": {"equals": "-1001730331343"}}}
        for spec in workflow["triggers"]]}
    for event in ("message.received", "channel_post.received",
                  "callback_query.received"):
        assert matches(scoped, {"connector": "telegram", "event": event,
                                "data": {"hook": "bot-inbox",
                                         "chat_id": -1001730331343}}), event
        assert not matches(scoped, {"connector": "telegram", "event": event,
                                    "data": {"hook": "bot-inbox",
                                             "chat_id": -42}}), event
    # an event-scoped workflow (designer-saved) can select one of the two
    only_posts = {"enabled": True, "actions": [], "triggers": [
        {"connector": "telegram", "event": "channel_post.received",
         "filters": {"hook": {"equals": "bot-inbox"}}}]}
    assert matches(only_posts, {"connector": "telegram",
                                "event": "channel_post.received",
                                "data": {"hook": "bot-inbox"}})
    assert not matches(only_posts, {"connector": "telegram",
                                    "event": "message.received",
                                    "data": {"hook": "bot-inbox"}})


# --- samples: one documented payload per event -----------------------------------


@pytest.fixture(autouse=True)
def no_recorded_history(monkeypatch):
    """Pin the fallback chain to synthetic: history depends on run tables."""
    monkeypatch.setattr(
        trigger_discovery, "history_sample",
        lambda connector, event=None: None)


def _sample(connector, **kwargs):
    status, payload = trigger_discovery.api_discover(
        {"connector": connector, **kwargs})
    assert status == 200, payload
    return payload


def _assert_envelope(sample, connector, event):
    for key in trigger_discovery.ENVELOPE_KEYS:
        assert key in sample, key
    assert sample["connector"] == connector
    assert sample["event"] == event
    assert isinstance(sample["data"], dict) and sample["data"]
    assert sample["id"] and sample["occurred_at"]


def test_telegram_sample_serves_both_events():
    message = _sample("telegram", event="message.received")["sample"]
    _assert_envelope(message, "telegram", "message.received")
    assert message["data"]["is_channel_post"] is False
    assert message["data"]["update"]["message"]["text"] == message["data"]["text"]

    post = _sample("telegram", event="channel_post.received")["sample"]
    _assert_envelope(post, "telegram", "channel_post.received")
    assert post["data"]["is_channel_post"] is True
    raw = post["data"]["update"]["channel_post"]
    assert raw["chat"]["title"] and raw["chat"]["type"] == "channel"
    assert raw["message_id"] == post["data"]["message_id"]
    assert raw["author_signature"]
    # the two events never answer with each other's payload
    assert post["data"]["update_id"] != message["data"]["update_id"]


def test_telegram_sample_serves_callback_queries():
    tap = _sample("telegram", event="callback_query.received")["sample"]
    _assert_envelope(tap, "telegram", "callback_query.received")
    data = tap["data"]
    # the flattened shape a real delivery publishes: the tap, the tapper,
    # and the originating message's chat fields
    assert data["id"] and data["data"] == "join:september"
    assert data["from"]["id"] and data["from"]["username"]
    assert data["message_id"] == 27
    assert data["chat_id"] == 555
    assert data["inline_message_id"] is None
    raw = data["update"]["callback_query"]
    assert raw["message"]["reply_markup"]["inline_keyboard"][0][0]["callback_data"] \
        == data["data"]
    # still a distinct payload from the other two events
    assert data["update_id"] != _sample("telegram", event="message.received")["sample"]["data"]["update_id"]


def test_telegram_sample_without_an_event_defaults_to_messages():
    default = _sample("telegram")["sample"]
    _assert_envelope(default, "telegram", "message.received")


def test_telegram_sample_history_never_crosses_events(monkeypatch):
    recorded = {"connector": "telegram", "event": "message.received",
                "data": {"text": "a real message"}, "id": "tg:real",
                "source": "bot-inbox", "occurred_at": "2026-09-28T09:00:00Z"}

    def fake_history(connector, event=None):
        return dict(recorded) if connector == "telegram" and event == "message.received" else None

    monkeypatch.setattr(trigger_discovery, "history_sample", fake_history)
    hit = _sample("telegram", event="message.received")
    assert hit["source"] == "history"
    assert hit["sample"]["data"]["text"] == "a real message"
    miss = _sample("telegram", event="channel_post.received")
    assert miss["source"] == "synthetic"
    assert miss["sample"]["event"] == "channel_post.received"
    assert miss["sample"]["data"]["is_channel_post"] is True


def test_zoom_registration_created_sample_carries_the_registrant():
    registration = _sample("zoom", event="meeting.registration_created")["sample"]
    _assert_envelope(registration, "zoom", "meeting.registration_created")
    data = registration["data"]
    assert data["email"] == "ada@example.test"
    assert data["registrant_id"] and data["status"]
    assert data["first_name"] and data["last_name"]
    assert data["topic"] and data["start_time"]
    # no other zoom event carries registrant fields
    started = _sample("zoom", event="meeting.started")["sample"]["data"]
    assert "email" not in started and "registrant_id" not in started


def test_unknown_events_fall_back_to_the_default_payload():
    telegram = _sample("telegram", event="bogus.event")["sample"]
    assert telegram["event"] == "message.received"
    zoom = _sample("zoom", event="meeting.participant_joined")["sample"]
    assert zoom["event"] == "recording.completed"


# --- the chips declare exactly what the intakes publish --------------------------


def test_chip_events_name_events_the_intakes_publish():
    from src.dapier.connectors.registry import CONNECTORS
    from src.dapier.triggers.intake import zoom_webhooks

    assert CONNECTORS["telegram"].events == \
        ("message.received", "channel_post.received", "callback_query.received")
    assert hook_triggers.TELEGRAM_CHANNEL_POST_EVENT in CONNECTORS["telegram"].events
    assert hook_triggers.TELEGRAM_CALLBACK_QUERY_EVENT in CONNECTORS["telegram"].events
    assert "meeting.registration_created" in CONNECTORS["zoom"].events
    assert "meeting.registration_created" in \
        zoom_webhooks.RECORDING_EVENTS + zoom_webhooks.MEETING_EVENTS \
        + zoom_webhooks.REGISTRATION_EVENTS
    # every chip event serves its own sample
    for connector in ("telegram", "zoom"):
        for event in CONNECTORS[connector].events:
            payload = _sample(connector, event=event)
            assert payload["sample"]["event"] == event, (connector, event)
