"""Zoom parity round: past-meetings discovery and the webinar set.

Piece 1 — past meetings: the provider listing gained a ``type`` window
(upcoming | previous_meetings) and a ``past_meetings`` resource, with the
registry/options twins and the discover markers the participants and
delete fields pick from; ``zoom_find_meeting`` gained a ``scope`` field
that switches the topic search between the same two windows.

Piece 2 — webinars: zoom_create_webinar / zoom_update_webinar /
zoom_find_webinar / zoom_add_webinar_registrant mirror the meeting runners
on Zoom's webinar endpoints; the chip declares webinar.started /
webinar.ended / webinar.registration_created, the intake maps them with
the meeting builders, and each declared event serves its own sample.

Unit tests drive the runners with a fake provider transport and the
connection/token seams patched (the test_round_yts3zoom pattern); the
discovery half follows test_discovery_zoom; the intake half follows
test_zoom's signed-delivery harness; the sample half follows
test_trigger_variety_zoom.
"""
import hashlib
import hmac
import json
import time
import unittest
import urllib.parse
from unittest.mock import patch

import pytest

from src.dapier.connectors import trigger_discovery
from src.dapier.connectors.registry import (
    CONNECTORS,
    DISCOVERIES,
    validate_action_chain,
)
from src.dapier.connections import discovery as provider
from src.dapier.connections import tokens
from src.dapier.engine.actions.zoom import (
    run_zoom_add_webinar_registrant,
    run_zoom_create_webinar,
    run_zoom_find_meeting,
    run_zoom_find_webinar,
    run_zoom_update_webinar,
)
from src.dapier.triggers.intake import zoom_webhooks

import src.dapier.connectors  # noqa: F401  (import = registration)


ZOOM_CONNECTION = {"connection_id": "zoom-main", "provider": "zoom",
                   "status": "connected", "credential_id": "oauth#zoom-main"}

EVENT = {"id": "evt/1", "connector": "schedule", "event": "schedule.fired",
         "occurred_at": "2026-09-28T09:00:00+00:00",
         "data": {"topic": "Product webinar"}}

MEETINGS_PAGE = {"meetings": [
    {"id": 9001, "topic": "Retro", "start_time": "2026-09-20T10:00:00Z",
     "join_url": "https://zoom.us/j/9001"}]}
WEBINARS_PAGE = {"webinars": [
    {"id": 98765432100, "topic": "Product webinar",
     "start_time": "2026-10-01T15:00:00Z",
     "join_url": "https://zoom.us/w/98765432100"}]}


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


def json_body(payload):
    return json.dumps(payload).encode()


def run_zoom_action(runner, action, event=None, steps=None, transport=None):
    action = {"type": runner.__name__.replace("run_", "", 1),
              "connection_id": "zoom-main", **action}
    with patch("src.dapier.engine.actions.zoom._zoom_connection",
               return_value=dict(ZOOM_CONNECTION)), \
         patch("src.dapier.connections.tokens.get_access_token",
               return_value=("tok", {})):
        return runner(action, event or EVENT, steps=steps, transport=transport)


# --- piece 1: the past_meetings listing and its windows --------------------------


@pytest.fixture(autouse=True)
def live_token(monkeypatch):
    monkeypatch.setattr(
        tokens, "get_access_token",
        lambda connection, transport=None: ("tok", {}))


def listing_transport(calls):
    def transport(method, url, *, headers=None, body=None, timeout=15):
        calls.append({"method": method, "url": url})
        return 200, json.dumps(MEETINGS_PAGE).encode()
    return transport


def _listing_query(call):
    return urllib.parse.parse_qs(urllib.parse.urlsplit(call["url"]).query)


def test_past_meetings_listing_fetches_the_previous_meetings_window():
    calls = []
    items = provider.discover(dict(ZOOM_CONNECTION), "past_meetings", {},
                              transport=listing_transport(calls))

    assert items[0] == {"id": "9001", "name": "Retro",
                        "start_time": "2026-09-20T10:00:00Z",
                        "join_url": "https://zoom.us/j/9001"}
    call, = calls
    assert call["url"].startswith("https://api.zoom.us/v2/users/me/meetings")
    assert _listing_query(call)["type"] == ["previous_meetings"]


def test_meetings_listing_keeps_the_upcoming_default():
    calls = []
    provider.discover(dict(ZOOM_CONNECTION), "meetings", {},
                      transport=listing_transport(calls))
    assert _listing_query(calls[0])["type"] == ["upcoming"]


def test_past_meetings_type_param_flips_the_window_and_bad_values_are_400():
    calls = []
    provider.discover(dict(ZOOM_CONNECTION), "past_meetings",
                      {"type": "upcoming"}, transport=listing_transport(calls))
    assert _listing_query(calls[0])["type"] == ["upcoming"]

    with pytest.raises(provider.DiscoveryError) as excinfo:
        provider.discover(dict(ZOOM_CONNECTION), "past_meetings",
                          {"type": "sideways"},
                          transport=listing_transport(calls))
    assert excinfo.value.status == 400
    assert "upcoming, previous_meetings" in str(excinfo.value)


def test_webinars_listing_hits_the_webinars_endpoint():
    calls = []

    def transport(method, url, *, headers=None, body=None, timeout=15):
        calls.append({"method": method, "url": url})
        return 200, json.dumps(WEBINARS_PAGE).encode()

    items = provider.discover(dict(ZOOM_CONNECTION), "webinars", {},
                              transport=transport)

    assert items == [{"id": "98765432100", "name": "Product webinar",
                      "start_time": "2026-10-01T15:00:00Z",
                      "join_url": "https://zoom.us/w/98765432100"}]
    assert calls[0]["url"].startswith("https://api.zoom.us/v2/users/me/webinars")
    assert _listing_query(calls[0])["type"] == ["upcoming"]


def test_registry_and_options_twins_exist_for_the_new_listings(monkeypatch):
    """The options-breadth invariant, named for the round: past_meetings and
    webinars registry listings each carry their zoom.* options twin, and the
    twins delegate to exactly those listings."""
    for name in ("past_meetings", "webinars"):
        assert DISCOVERIES[f"zoom.{name}"].run is not None
        twin = trigger_discovery.TRIGGER_DISCOVERIES.get(
            ("zoom", "options", f"zoom.{name}"))
        assert twin is not None and callable(twin.fetch), f"zoom.{name}"

    seen = []

    def fake(resource, connection_id, limit, **kwargs):
        seen.append((resource, connection_id, limit))
        return {"options": [{"value": "1", "label": "One"}],
                "connection_id": connection_id}

    monkeypatch.setattr(trigger_discovery, "options_from_registry", fake)
    for name in ("past_meetings", "webinars"):
        payload = trigger_discovery.TRIGGER_DISCOVERIES[
            ("zoom", "options", f"zoom.{name}")].fetch(
            connection_id="zoom-main", limit=5)
        assert payload["options"] == [{"value": "1", "label": "One"}]
    assert seen == [("zoom.past_meetings", "zoom-main", 5),
                    ("zoom.webinars", "zoom-main", 5)]


# --- piece 1: the find action's scope field --------------------------------------


def test_find_meeting_topic_search_defaults_to_upcoming():
    transport = FakeTransport(("users/me/meetings", 200, json_body(MEETINGS_PAGE)))

    output = run_zoom_action(
        run_zoom_find_meeting, {"topic": "Retro"}, transport=transport)

    assert output["found"] is True
    assert output["scope"] == "upcoming"
    call, = transport.calls
    assert _listing_query(call)["type"] == ["upcoming"]


def test_find_meeting_scope_past_searches_previous_meetings():
    transport = FakeTransport(("users/me/meetings", 200, json_body(MEETINGS_PAGE)))

    output = run_zoom_action(
        run_zoom_find_meeting, {"topic": "Retro", "scope": "past"},
        transport=transport)

    assert output == {"found": True, "meeting": {
        "id": "9001", "topic": "Retro", "start_time": "2026-09-20T10:00:00Z",
        "join_url": "https://zoom.us/j/9001", "duration": None},
        "matched_by": "topic", "scope": "past"}
    assert _listing_query(transport.calls[0])["type"] == ["previous_meetings"]


def test_find_meeting_rejects_an_unknown_scope():
    transport = FakeTransport(("users/me/meetings", 200, json_body(MEETINGS_PAGE)))

    with pytest.raises(ValueError, match="scope must be one of"):
        run_zoom_action(run_zoom_find_meeting,
                        {"topic": "Retro", "scope": "sideways"},
                        transport=transport)
    assert transport.calls == []


def test_find_meeting_create_if_missing_needs_the_upcoming_scope():
    transport = FakeTransport(("users/me/meetings", 200, json_body(MEETINGS_PAGE)))

    with pytest.raises(ValueError, match="upcoming scope"):
        run_zoom_action(run_zoom_find_meeting,
                        {"topic": "Retro", "scope": "past",
                         "create_if_missing": "true"},
                        transport=transport)
    assert transport.calls == []  # nothing listed, nothing created


# --- piece 2: the webinar runners ------------------------------------------------


class ZoomWebinarRunnerTests(unittest.TestCase):
    def test_create_schedules_a_webinar(self):
        transport = FakeTransport(("users/me/webinars", 200, json_body({
            "id": 98765432100, "topic": "Product webinar",
            "start_time": "2026-10-01T09:00:00Z", "duration": 45,
            "join_url": "https://zoom.us/w/98765432100",
            "start_url": "https://zoom.us/s/98765432100",
            "password": "secret1"})))

        output = run_zoom_action(
            run_zoom_create_webinar,
            {"topic": "Product webinar", "start_time": "2026-10-01T09:00:00Z",
             "duration": "45", "timezone": "Europe/Berlin",
             "agenda": "Launch", "settings": '{{"approval_type": 2}}'},
            transport=transport)

        assert output == {"created": True, "scheduled": True, "webinar": {
            "id": "98765432100", "topic": "Product webinar",
            "start_time": "2026-10-01T09:00:00Z", "duration": 45,
            "join_url": "https://zoom.us/w/98765432100",
            "start_url": "https://zoom.us/s/98765432100",
            "passcode": "secret1"}}
        call, = transport.calls
        assert call["method"] == "POST"
        assert call["url"] == "https://api.zoom.us/v2/users/me/webinars"
        assert json.loads(call["body"]) == {
            "topic": "Product webinar", "type": 5,
            "start_time": "2026-10-01T09:00:00Z", "duration": 45,
            "timezone": "Europe/Berlin", "agenda": "Launch",
            "settings": {"approval_type": 2}}

    def test_create_without_a_start_time_is_recurring_with_no_fixed_time(self):
        transport = FakeTransport(("users/me/webinars", 200, json_body({
            "id": 98765432100, "topic": "Office hours",
            "join_url": "https://zoom.us/w/98765432100"})))

        output = run_zoom_action(run_zoom_create_webinar,
                                 {"topic": "Office hours"},
                                 transport=transport)

        assert output["scheduled"] is False
        assert json.loads(transport.calls[0]["body"])["type"] == 6

    def test_create_requires_a_topic(self):
        transport = FakeTransport()
        with pytest.raises(ValueError, match="requires topic"):
            run_zoom_action(run_zoom_create_webinar, {}, transport=transport)
        assert transport.calls == []

    def test_update_sends_only_the_filled_fields(self):
        transport = FakeTransport(("/webinars/8999", 204, b""))

        output = run_zoom_action(
            run_zoom_update_webinar,
            {"webinar_id": "8999", "start_time": "2026-10-02T09:00:00Z",
             "duration": "45"},
            transport=transport)

        assert output == {"updated": True, "webinar_id": "8999",
                          "updated_fields": ["duration", "start_time"]}
        call, = transport.calls
        assert call["method"] == "PATCH"
        assert call["url"] == "https://api.zoom.us/v2/webinars/8999"
        assert json.loads(call["body"]) == {
            "start_time": "2026-10-02T09:00:00Z", "duration": 45}

    def test_update_with_nothing_to_send_is_an_error(self):
        transport = FakeTransport(("/webinars/8999", 204, b""))

        with pytest.raises(ValueError, match="needs at least one of"):
            run_zoom_action(run_zoom_update_webinar, {"webinar_id": "8999"},
                            transport=transport)
        assert transport.calls == []

    def test_find_by_id_is_a_verdict_when_dead(self):
        transport = FakeTransport(("/webinars/8999", 404, b"{}"))

        output = run_zoom_action(run_zoom_find_webinar,
                                 {"webinar_id": "8999"},
                                 transport=transport)
        assert output == {"found": False, "webinar": None}

        transport = FakeTransport(("/webinars/8999", 200, json_body({
            "id": 8999, "topic": "Product webinar", "duration": 60,
            "join_url": "https://zoom.us/w/8999"})))
        output = run_zoom_action(run_zoom_find_webinar,
                                 {"webinar_id": "8999"},
                                 transport=transport)
        assert output["found"] is True
        assert output["webinar"]["id"] == "8999"

    def test_find_by_topic_searches_the_scope_window(self):
        transport = FakeTransport(
            ("users/me/webinars", 200, json_body(WEBINARS_PAGE)))

        output = run_zoom_action(run_zoom_find_webinar,
                                 {"topic": "Product"},
                                 transport=transport)

        assert output["found"] is True
        assert output["webinar"]["id"] == "98765432100"
        assert output["scope"] == "upcoming"
        assert _listing_query(transport.calls[0])["type"] == ["upcoming"]

        past = FakeTransport(
            ("users/me/webinars", 200, json_body(WEBINARS_PAGE)))
        output = run_zoom_action(run_zoom_find_webinar,
                                 {"topic": "Product", "scope": "past"},
                                 transport=past)
        assert _listing_query(past.calls[0])["type"] == ["previous_meetings"]
        assert output["scope"] == "past"

    def test_add_webinar_registrant_returns_the_personalized_link(self):
        transport = FakeTransport(
            ("/webinars/8999/registrants", 201, json_body({
                "registrant_id": "reg-1",
                "join_url": "https://zoom.us/w/8999/reg-1"})))

        output = run_zoom_action(
            run_zoom_add_webinar_registrant,
            {"webinar_id": "8999", "email": "ada@example.test",
             "first_name": "Ada"},
            transport=transport)

        assert output == {"registered": True, "webinar_id": "8999",
                          "registrant_id": "reg-1",
                          "join_url": "https://zoom.us/w/8999/reg-1"}
        call, = transport.calls
        assert call["method"] == "POST"
        assert call["url"] == "https://api.zoom.us/v2/webinars/8999/registrants"
        assert json.loads(call["body"]) == {"email": "ada@example.test",
                                            "first_name": "Ada"}

    def test_add_webinar_registrant_requires_email(self):
        transport = FakeTransport()
        with pytest.raises(ValueError, match="requires email"):
            run_zoom_action(run_zoom_add_webinar_registrant,
                            {"webinar_id": "8999"}, transport=transport)
        assert transport.calls == []


# --- piece 2: the chip, the intake, and the samples ------------------------------


class Table:
    def __init__(self):
        self.items = {}

    def get_item(self, Key):
        return {"Item": self.items[Key["connection_id"]]} \
            if Key["connection_id"] in self.items else {}

    def put_item(self, Item):
        self.items[Item["connection_id"]] = dict(Item)


def signed(message, secret="zoom-secret-123456", timestamp=None):
    body = json.dumps(message, separators=(",", ":")).encode()
    stamp = str(timestamp or int(time.time()))
    digest = hmac.new(secret.encode(), b"v0:" + stamp.encode() + b":" + body,
                      hashlib.sha256).hexdigest()
    return body, {"X-Zm-Request-Timestamp": stamp, "X-Zm-Signature": f"v0={digest}"}


@pytest.fixture
def webhook_connection(monkeypatch):
    """A webhook-only Zoom connection, the test_zoom.py setup."""
    from src.dapier.connections import zoom as zoom_connection_setup

    table = Table()
    secrets = {}
    monkeypatch.setattr(zoom_connection_setup.credentials, "get_credential",
                        lambda key: secrets[key])
    monkeypatch.setattr(zoom_connection_setup.credentials, "put_credential",
                        lambda key, value, **_: secrets.__setitem__(key, value))
    status, _public = zoom_connection_setup.save(
        {"connection_id": "zoom", "provider": "zoom", "display_name": "Zoom",
         "token": "zoom-secret-123456"},
        operator_subject="operator", connections_table=table)
    assert status == 200
    return table, secrets


def _handle(table, body, headers, publish):
    return zoom_webhooks.handle("zoom", headers, body,
                                connections_table=table, publish=publish)


def _webinar_lifecycle(event, event_ts, **extra):
    payload = {"account_id": "account-1", "object": {
        "id": 9876, "uuid": "webinar-uuid", "topic": "Product webinar",
        "host_id": "host-1", "start_time": "2026-10-01T15:00:00Z",
        "duration": 60, "timezone": "Europe/Berlin",
        "settings": {"approval_type": 2}}}
    payload["object"].update(extra)
    return {"event": event, "event_ts": event_ts, "payload": payload}


def test_webinar_lifecycle_deliveries_publish_metadata_only(webhook_connection):
    table, _secrets = webhook_connection
    published = []
    publish = lambda *args, **kwargs: published.append((args, kwargs))  # noqa: E731

    started = _webinar_lifecycle("webinar.started", 3000)
    ended = _webinar_lifecycle("webinar.ended", 3060000,
                               end_time="2026-10-01T16:02:00Z")
    for message in (started, ended):
        body, headers = signed(message)
        assert _handle(table, body, headers, publish) == (200, {"accepted": True})

    assert [args[1] for args, _ in published] == \
        ["webinar.started", "webinar.ended"]
    started_data, ended_data = published[0][0][2], published[1][0][2]
    assert started_data == {
        "connection_id": "zoom", "account_id": "account-1",
        "uuid": "webinar-uuid", "id": 9876, "topic": "Product webinar",
        "host_id": "host-1", "start_time": "2026-10-01T15:00:00Z",
        "duration": 60, "timezone": "Europe/Berlin"}
    assert "end_time" not in started_data
    assert ended_data["end_time"] == "2026-10-01T16:02:00Z"
    dumped = json.dumps([started_data, ended_data])
    assert "approval_type" not in dumped and "object" not in dumped
    # distinct lifecycle moments never share a dedup id
    assert published[0][1]["event_id"] != published[1][1]["event_id"]


def test_webinar_registration_created_publishes_the_registrant(webhook_connection):
    table, _secrets = webhook_connection
    published = []
    publish = lambda *args, **kwargs: published.append((args, kwargs))  # noqa: E731
    message = {"event": "webinar.registration_created", "event_ts": 8000,
               "payload": {"account_id": "account-1", "object": {
                   "id": 9876, "uuid": "webinar-uuid",
                   "topic": "Product webinar",
                   "start_time": "2026-10-01T15:00:00Z",
                   "timezone": "Europe/Berlin",
                   "registrant": {"id": "registrant-9",
                                  "email": "ada@example.test",
                                  "first_name": "Ada",
                                  "last_name": "Lovelace",
                                  "status": "approved"}}}}
    body, headers = signed(message)
    assert _handle(table, body, headers, publish) == (200, {"accepted": True})
    assert published[0][0][:2] == ("zoom", "webinar.registration_created")
    data = published[0][0][2]
    assert data == {"connection_id": "zoom", "account_id": "account-1",
                    "meeting_id": 9876, "meeting_uuid": "webinar-uuid",
                    "topic": "Product webinar",
                    "start_time": "2026-10-01T15:00:00Z",
                    "timezone": "Europe/Berlin",
                    "registrant_id": "registrant-9",
                    "email": "ada@example.test", "first_name": "Ada",
                    "last_name": "Lovelace", "status": "approved"}
    dumped = json.dumps(data)
    assert "join_url" not in dumped and "object" not in dumped
    # a retry of the same delivery keeps one dedup identity
    assert _handle(table, body, headers, publish) == (200, {"accepted": True})
    assert published[1][1]["event_id"] == published[0][1]["event_id"]


def test_webinar_intake_rejects_incomplete_and_unsubscribed_events(webhook_connection):
    table, _secrets = webhook_connection
    published = []
    publish = lambda *args, **kwargs: published.append((args, kwargs))  # noqa: E731

    incomplete = _webinar_lifecycle("webinar.started", 5000)
    incomplete["payload"]["object"]["uuid"] = None
    body, headers = signed(incomplete)
    assert _handle(table, body, headers, publish) == \
        (400, {"error": "incomplete Zoom meeting event"})

    body, headers = signed({"event": "webinar.participant_joined",
                            "event_ts": 5001,
                            "payload": {"account_id": "account-1",
                                        "object": {"id": 9876,
                                                   "uuid": "webinar-uuid"}}})
    assert _handle(table, body, headers, publish) == (200, {"accepted": False})
    assert published == []


def test_chip_events_match_the_intake_and_each_serves_a_sample(monkeypatch):
    monkeypatch.setattr(trigger_discovery, "history_sample",
                        lambda connector, event=None: None)
    declared = ("recording.completed", "recording.transcript_completed",
                "meeting.started", "meeting.ended",
                "meeting.registration_created",
                "webinar.started", "webinar.ended",
                "webinar.registration_created")
    assert CONNECTORS["zoom"].events == declared
    accepted = set(zoom_webhooks.RECORDING_EVENTS
                   + zoom_webhooks.MEETING_EVENTS + zoom_webhooks.WEBINAR_EVENTS
                   + zoom_webhooks.REGISTRATION_EVENTS
                   + zoom_webhooks.WEBINAR_REGISTRATION_EVENTS)
    assert set(declared) == accepted
    for event in declared:
        status, payload = trigger_discovery.api_discover(
            {"connector": "zoom", "event": event})
        assert status == 200, (event, payload)
        assert payload["sample"]["event"] == event, event
        assert payload["source"] == "synthetic", event


def test_webinar_samples_differ_from_their_meeting_twins(monkeypatch):
    monkeypatch.setattr(trigger_discovery, "history_sample",
                        lambda connector, event=None: None)

    def sample(event):
        status, payload = trigger_discovery.api_discover(
            {"connector": "zoom", "event": event})
        assert status == 200, payload
        return payload["sample"]["data"]

    webinar, meeting = sample("webinar.started"), sample("meeting.started")
    assert webinar["id"] != meeting["id"]
    assert webinar["topic"] == "Product webinar"
    registration = sample("webinar.registration_created")
    assert registration["email"] == "ada@example.test"
    assert "join_url" not in json.dumps(registration)
    # the unknown-event fallback stays on the default recording payload
    unknown = trigger_discovery.api_discover(
        {"connector": "zoom", "event": "webinar.participant_joined"})[1]
    assert unknown["sample"]["event"] == "recording.completed"


# --- registry specs: chains validate against the new actions ---------------------


def test_webinar_action_chains_validate():
    chain = [
        {"type": "zoom_create_webinar", "connection_id": "zoom",
         "topic": "Product webinar", "settings": '{{"approval_type": 2}}'},
        {"type": "zoom_add_webinar_registrant", "connection_id": "zoom",
         "webinar_id": "{steps.create.output.webinar.id}",
         "email": "{trigger.email}"},
        {"type": "zoom_find_webinar", "connection_id": "zoom",
         "topic": "Product webinar", "scope": "past"},
    ]
    assert validate_action_chain(chain) == chain

    with pytest.raises(Exception):
        validate_action_chain([{"type": "zoom_create_webinar",
                                "connection_id": "zoom"}])  # topic missing
    with pytest.raises(Exception):
        validate_action_chain([{"type": "zoom_find_webinar",
                                "connection_id": "zoom",
                                "topic": "x", "scope": "sideways"}])
    with pytest.raises(Exception):
        validate_action_chain([{"type": "zoom_update_webinar",
                                "connection_id": "zoom",
                                "webinar_id": "9", "bogus": 1}])
