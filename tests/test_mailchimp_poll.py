"""The mailchimp.members poll source: save validation, the last_changed
watermark fetch (seeded first fire, oldest first, per-member dedupe), the
end-to-end fire, and the chip's sample pull with the stored-poll live
branch ahead of the per-event webhook chain.

Zapier's "New Subscriber" without a Mailchimp webhook: a stored poll
trigger with ``source: "mailchimp.members"`` lists the audience through
the Marketing API with the stored key (no OAuth connection required) and
publishes the chip's ``mailchimp``/``member.new`` events. Transport is
stubbed at the actions layer's default transport — the same basic-auth
request seam the ping test and actions use.
"""
import json
from unittest.mock import patch

import pytest

from src.dapier.connectors import mailchimp as mailchimp_connector
from src.dapier.connectors import trigger_discovery
from src.dapier.engine.actions import base
from src.dapier.connections import credentials
from src.dapier.triggers import poll_sources, poll_triggers
from src.dapier.triggers.email_triggers import TriggerError

LIST_ID = "abc123"


def members_page(*members):
    """A members listing body, one entry per given
    (email, last_changed, status, first_name)."""
    return {"members": [
        {"id": f"contact-{email}", "email_address": email,
         "status": status, "last_changed": changed,
         "merge_fields": {"FNAME": first, "LNAME": "Example"}}
        for email, changed, status, first in members]}


class Transport:
    """Canned Marketing API pages, recording every call."""

    def __init__(self, page=None, status=200):
        self.page = page if page is not None else {"members": []}
        self.status = status
        self.calls = []

    def __call__(self, method, url, *, headers=None, body=None, timeout=15):
        self.calls.append({"method": method, "url": url, "headers": headers})
        return self.status, json.dumps(self.page).encode()


def mailchimp_body(**overrides):
    body = {
        "name": "audience-members",
        "expression": "rate(1 hour)",
        "source": "mailchimp.members",
        "list_id": LIST_ID,
        "actions": [{"type": "email_send", "to": "ops@example.test"}],
    }
    body.update(overrides)
    return body


def stored(body):
    """The stored item build_item would persist for ``body``."""
    return poll_triggers.build_item(body, "op@example.test")


def stub_credentials(monkeypatch):
    """The shared ``mailchimp`` credential resolves without DynamoDB."""
    monkeypatch.setattr(credentials, "get_credential",
                        lambda credential_id: {"apiKey": "key123-us21",
                                               "server": "us21"})


def run_fire(item, transport, *, cursors=None):
    """One scheduled fire against a canned listing, with real cursor/seen
    machinery and the engine stubbed out."""
    fired_events = []
    cursors = cursors or FakeCursorTable()

    with patch.object(poll_triggers, "get_item", return_value=item), \
         patch.object(credentials, "get_credential",
                      return_value={"apiKey": "key123-us21", "server": "us21"}), \
         patch.object(base, "_default_transport", transport), \
         patch("src.dapier.engine.execute",
               side_effect=lambda event, **_kwargs: fired_events.append(event)), \
         patch("src.dapier.engine.notify.notify_failure"):
        result = poll_triggers.fire(item["poll_id"], cursor_table_ref=cursors)
    return result, fired_events, cursors


class FakeCursorTable:
    """DynamoDB stand-in for poll cursors and seen-sets."""

    def __init__(self):
        self.items = {}

    @staticmethod
    def _partition(key_or_item):
        return (key_or_item.get("cursor_id")
                if "cursor_id" in key_or_item else key_or_item.get("scope_id"))

    def get_item(self, Key):
        item = self.items.get(self._partition(Key))
        return {"Item": dict(item)} if item else {}

    def put_item(self, Item):
        self.items[self._partition(Item)] = dict(Item)

    def delete_item(self, Key):
        self.items.pop(self._partition(Key), None)


# --- registration -----------------------------------------------------------------


def test_the_mailchimp_chip_registers_a_poll_source():
    source = poll_sources.SOURCES["mailchimp.members"]

    assert (source.connector, source.event) == ("mailchimp", "member.new")
    assert "mailchimp.members" in poll_sources.source_names()


# --- save validation --------------------------------------------------------------


def test_save_stores_the_fetch_spec():
    item = stored(mailchimp_body())

    assert item["source"] == "mailchimp.members"
    assert item["list_id"] == LIST_ID
    assert item["connection_id"] == ""  # the shared credential is the default
    assert item["cursor_mode"] == "next_cursor"
    assert item["id_path"] == "id"
    assert item["url"] == ""  # non-http sources never carry an endpoint


def test_save_requires_the_audience():
    with pytest.raises(TriggerError, match="list_id"):
        stored(mailchimp_body(list_id=""))


def test_a_connection_id_is_stored_when_given():
    item = stored(mailchimp_body(connection_id="mailchimp-main"))

    assert item["connection_id"] == "mailchimp-main"


def test_public_view_shows_the_audience():
    view = poll_triggers.public_view(stored(mailchimp_body()))

    assert view["source"] == "mailchimp.members"
    assert view["list_id"] == LIST_ID


# --- fetch: seed, then strictly-newer members ----------------------------------------


def test_first_fetch_seeds_at_the_newest_member_without_emitting(monkeypatch):
    stub_credentials(monkeypatch)
    transport = Transport(members_page(
        ("old@example.test", "2026-09-26T10:00:00+00:00", "subscribed", "Old"),
        ("new@example.test", "2026-09-28T10:00:00+00:00", "subscribed", "New")))
    item = stored(mailchimp_body())

    members, next_cursor = mailchimp_connector._mailchimp_poll_fetch(
        item, None, transport=transport)

    assert members == []
    assert next_cursor == "2026-09-28T10:00:00+00:00|contact-new@example.test"  # composite last_changed|id watermark
    call = transport.calls[0]
    assert "/lists/abc123/members" in call["url"]
    assert "sort_field=last_changed" in call["url"]
    assert "sort_dir=DESC" in call["url"]
    assert call["headers"]["authorization"].startswith("Basic ")


def test_next_fetch_returns_only_newer_members_oldest_first(monkeypatch):
    stub_credentials(monkeypatch)
    # The listing comes back DESC from Mailchimp; the fetch reorders.
    transport = Transport(members_page(
        ("third@example.test", "2026-09-28T12:00:00+00:00", "subscribed", "T"),
        ("first@example.test", "2026-09-28T10:00:00+00:00", "pending", "F"),
        ("second@example.test", "2026-09-28T11:00:00+00:00", "subscribed", "S")))
    item = stored(mailchimp_body())

    members, next_cursor = mailchimp_connector._mailchimp_poll_fetch(
        item, "2026-09-28T10:00:00+00:00|contact-first@example.test", transport=transport)

    assert [member["email"] for member in members] == [
        "second@example.test", "third@example.test"]
    assert next_cursor == "2026-09-28T12:00:00+00:00|contact-third@example.test"


def test_nothing_new_keeps_the_cursor(monkeypatch):
    stub_credentials(monkeypatch)
    transport = Transport(members_page(
        ("old@example.test", "2026-09-26T10:00:00+00:00", "subscribed", "Old")))
    item = stored(mailchimp_body())

    members, next_cursor = mailchimp_connector._mailchimp_poll_fetch(
        item, "2026-09-28T10:00:00+00:00", transport=transport)

    assert members == []
    assert next_cursor == "2026-09-28T10:00:00+00:00"


def test_items_carry_the_event_data_shape(monkeypatch):
    stub_credentials(monkeypatch)
    transport = Transport(members_page(
        ("reader@example.test", "2026-09-28T10:00:00+00:00", "subscribed", "Reader")))
    item = stored(mailchimp_body())

    members, _next = mailchimp_connector._mailchimp_poll_fetch(
        item, "2026-09-27T00:00:00+00:00", transport=transport)

    assert members == [{
        "id": "contact-reader@example.test",
        "email": "reader@example.test",
        "status": "subscribed",
        "last_changed": "2026-09-28T10:00:00+00:00",
        "first_name": "Reader",
        "last_name": "Example",
    }]


def test_a_stored_poll_without_a_list_id_never_lists(monkeypatch):
    stub_credentials(monkeypatch)

    with pytest.raises(RuntimeError, match="list_id"):
        mailchimp_connector._mailchimp_poll_fetch({"poll_id": "x"}, None)


def test_a_failed_listing_raises_runtimeerror(monkeypatch):
    stub_credentials(monkeypatch)
    transport = Transport(page={"detail": "bad key"}, status=401)
    item = stored(mailchimp_body())

    with pytest.raises(RuntimeError, match="HTTP 401"):
        mailchimp_connector._mailchimp_poll_fetch(
            item, "2026-09-28T10:00:00+00:00", transport=transport)


def test_a_named_connection_is_used_for_the_fetch(monkeypatch):
    monkeypatch.setattr(credentials, "get_credential",
                        lambda credential_id: {"apiKey": "conn-key-us7",
                                               "server": "us7"})
    transport = Transport(members_page(
        ("reader@example.test", "2026-09-28T10:00:00+00:00", "subscribed", "R")))
    item = stored(mailchimp_body(connection_id="mailchimp-main"))

    with patch("src.dapier.engine.actions.base._connected_connection",
               return_value={"connection_id": "mailchimp-main",
                             "credential_id": "conn-key"}):
        mailchimp_connector._mailchimp_poll_fetch(
            item, "2026-09-27T00:00:00+00:00", transport=transport)

    assert "us7.api.mailchimp.com" in transport.calls[0]["url"]


# --- end-to-end fire ---------------------------------------------------------------


def test_fire_seeds_then_emits_only_the_new_member():
    item = stored(mailchimp_body())
    cursors = FakeCursorTable()
    empty = Transport(members_page(
        ("old@example.test", "2026-09-26T10:00:00+00:00", "subscribed", "Old")))

    result, fired, cursors = run_fire(item, empty, cursors=cursors)

    assert result == {"poll": "audience-members", "fired": 0}
    assert fired == []
    assert poll_triggers.get_cursor("audience-members",
                                    table=cursors) == "2026-09-26T10:00:00+00:00|contact-old@example.test"

    growing = Transport(members_page(
        ("old@example.test", "2026-09-26T10:00:00+00:00", "subscribed", "Old"),
        ("new@example.test", "2026-09-28T10:00:00+00:00", "subscribed", "New")))
    result, fired, _ = run_fire(item, growing, cursors=cursors)

    assert result == {"poll": "audience-members", "fired": 1}
    event = fired[0]
    assert event["connector"] == "mailchimp"
    assert event["event"] == "member.new"
    assert event["source"] == "audience-members"
    assert event["data"]["item_id"] == "contact-new@example.test"
    assert event["data"]["email"] == "new@example.test"
    assert poll_triggers.get_cursor("audience-members",
                                    table=cursors) == "2026-09-28T10:00:00+00:00|contact-new@example.test"


def test_a_relisted_member_does_not_fire_twice():
    """The seen-set dedupes the member id: a profile edit that bumps
    last_changed past the cursor is recognized as an already-seen member —
    the event is member.new, not member.updated."""
    item = stored(mailchimp_body())
    cursors = FakeCursorTable()
    run_fire(item, Transport(members_page(
        ("old@example.test", "2026-09-26T10:00:00+00:00", "subscribed", "Old"))),
        cursors=cursors)

    joined = Transport(members_page(
        ("reader@example.test", "2026-09-28T10:00:00+00:00", "subscribed", "R")))
    result, fired, cursors = run_fire(item, joined, cursors=cursors)
    assert result == {"poll": "audience-members", "fired": 1}

    edited = Transport(members_page(
        ("reader@example.test", "2026-09-29T08:00:00+00:00", "subscribed", "R")))
    result, fired, _ = run_fire(item, edited, cursors=cursors)

    assert result == {"poll": "audience-members", "fired": 0,
                      "skipped_seen": 1}
    assert fired == []


def test_the_fired_workflow_matches_the_chip():
    workflow = poll_triggers.workflow_for(stored(mailchimp_body()))

    assert workflow["trigger"] == {
        "connector": "mailchimp", "event": "member.new",
        "filters": {"poll": {"equals": "audience-members"}}}


# --- the chip's sample pull --------------------------------------------------------


def pin_no_history(monkeypatch):
    monkeypatch.setattr(trigger_discovery, "history_sample",
                        lambda connector, event=None: None)


def discover(**body):
    status, payload = trigger_discovery.api_discover({"kind": "sample", **body})
    return status, payload


def test_sample_pulls_the_polls_newest_member_live(monkeypatch):
    pin_no_history(monkeypatch)
    stub_credentials(monkeypatch)
    item = stored(mailchimp_body())
    monkeypatch.setattr(poll_triggers, "get_item",
                        lambda name, table_ref=None: item)
    monkeypatch.setattr(base, "_default_transport", Transport(members_page(
        ("old@example.test", "2026-09-26T10:00:00+00:00", "subscribed", "Old"),
        ("new@example.test", "2026-09-28T10:00:00+00:00", "subscribed", "New"))))

    status, payload = discover(connector="mailchimp", event="audience-members")

    assert status == 200, payload
    assert payload["source"] == "live"
    assert payload["sample"]["connector"] == "mailchimp"
    assert payload["sample"]["event"] == "member.new"
    assert payload["sample"]["data"]["email"] == "new@example.test"
    assert payload["sample"]["data"]["poll"] == "audience-members"


def test_sample_without_a_matching_poll_is_the_documented_member(monkeypatch):
    pin_no_history(monkeypatch)
    monkeypatch.setattr(poll_triggers, "get_item",
                        lambda name, table_ref=None: None)

    status, payload = discover(connector="mailchimp", event="member.new")

    assert status == 200, payload
    assert payload["source"] == "synthetic"
    assert payload["sample"]["event"] == "member.new"
    assert payload["sample"]["data"]["email"]
    assert payload["sample"]["data"]["last_changed"]

    # A dotted ask never names a poll, so it never goes live either.
    status, payload = discover(connector="mailchimp", event="no.such")
    assert status == 200, payload
    assert payload["source"] == "synthetic"
    assert payload["sample"]["event"] == "subscribe"  # the documented default


def test_sample_ignores_a_poll_with_another_source(monkeypatch):
    pin_no_history(monkeypatch)
    item = stored(mailchimp_body(source="http", url="https://example.test/list",
                                 id_path="id"))
    monkeypatch.setattr(poll_triggers, "get_item",
                        lambda name, table_ref=None: item)

    status, payload = discover(connector="mailchimp", event="audience-members")

    assert status == 200, payload
    assert payload["source"] == "synthetic"


def test_sample_for_a_failing_members_poll_names_member_new(monkeypatch):
    """A stored poll whose live fetch fails falls back to the poll's own
    event — a member.new ask is never answered with a webhook-type payload."""
    pin_no_history(monkeypatch)
    stub_credentials(monkeypatch)
    item = stored(mailchimp_body())
    monkeypatch.setattr(poll_triggers, "get_item",
                        lambda name, table_ref=None: item)
    monkeypatch.setattr(base, "_default_transport", Transport(status=500))

    status, payload = discover(connector="mailchimp", event="audience-members")

    assert status == 200, payload
    assert payload["source"] == "synthetic"
    assert payload["sample"]["event"] == "member.new"


def test_sample_webhook_type_asks_keep_the_per_event_payloads(monkeypatch):
    pin_no_history(monkeypatch)

    status, payload = discover(connector="mailchimp", event="upemail")
    assert payload["sample"]["event"] == "upemail"
    assert payload["sample"]["data"]["type"] == "upemail"

    status, payload = discover(connector="mailchimp", event="subscribe")
    assert payload["sample"]["event"] == "subscribe"
    assert payload["sample"]["data"]["type"] == "subscribe"


if __name__ == "__main__":
    pytest.main([__file__])
