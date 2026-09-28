"""Trigger inbox domain behavior: record, close-out, read, replay."""
import json

import boto3
import pytest
from botocore.exceptions import ClientError

from src.dapier.triggers import inbox


EVENT = {
    "schema_version": "1.0",
    "id": "evt-1",
    "connector": "webhook",
    "event": "hook",
    "source": "github",
    "occurred_at": "2026-09-27T10:00:00+00:00",
    "data": {"action": "opened"},
}


class FakeTable:
    """put_item honours the dedupe condition; scan reads what was stored,
    honouring the page-size cap like the real bounded scan."""

    def __init__(self):
        self.items = {}
        self.updates = []

    def put_item(self, Item, **kwargs):
        if "ConditionExpression" in kwargs and Item["inbox_id"] in self.items:
            raise ClientError({"Error": {"Code": "ConditionalCheckFailedException"}},
                              "PutItem")
        self.items[Item["inbox_id"]] = dict(Item)

    def update_item(self, Key, UpdateExpression=None, ExpressionAttributeNames=None,
                    ExpressionAttributeValues=None, **kwargs):
        self.updates.append((Key["inbox_id"], UpdateExpression,
                             ExpressionAttributeNames, ExpressionAttributeValues))
        item = self.items.setdefault(Key["inbox_id"], {"inbox_id": Key["inbox_id"]})
        names = ExpressionAttributeNames or {}
        for placeholder, value in (ExpressionAttributeValues or {}).items():
            attribute = placeholder[1:]
            attribute = names.get(f"#{attribute}", attribute)
            item[attribute] = value

    def get_item(self, Key, **kwargs):
        item = self.items.get(Key["inbox_id"])
        return {"Item": dict(item)} if item else {}

    def scan(self, **kwargs):
        items = [dict(item) for item in self.items.values()]
        limit = kwargs.get("Limit")
        return {"Items": items[:limit] if limit else items}


class FakeQueue:
    def __init__(self):
        self.messages = []

    def send_message(self, QueueUrl, MessageBody):
        self.messages.append((QueueUrl, MessageBody))


@pytest.fixture
def table(monkeypatch):
    fake = FakeTable()
    monkeypatch.setenv(inbox.TABLE_ENV, "trigger-inbox")

    class Dynamo:
        def Table(self, _name):
            return fake

    monkeypatch.setattr(boto3, "resource", lambda service: Dynamo())
    return fake


def test_record_requires_an_event_id(monkeypatch):
    monkeypatch.setenv(inbox.TABLE_ENV, "trigger-inbox")
    monkeypatch.setattr(boto3, "resource",
                        lambda service: (_ for _ in ()).throw(AssertionError("no table")))
    assert inbox.record({"connector": "webhook"}) is None


def test_record_noops_when_the_inbox_is_not_configured(monkeypatch):
    monkeypatch.delenv(inbox.TABLE_ENV, raising=False)
    monkeypatch.setattr(boto3, "resource",
                        lambda service: (_ for _ in ()).throw(AssertionError("no table")))
    assert inbox.record(EVENT) is None
    assert inbox.complete("evt-1", ["wf"]) is None


def test_record_dedupes_on_the_event_id(table):
    assert inbox.record(EVENT, table_ref=table) == "evt-1"
    assert inbox.record(EVENT, table_ref=table) is None
    assert list(table.items) == ["evt-1"]
    stored = table.items["evt-1"]
    assert stored["status"] == inbox.RECEIVED
    assert stored["data"] == {"action": "opened"}
    assert stored["expires_at"] > 0


def test_complete_marks_matched_unmatched_and_failed(table):
    inbox.record(EVENT, table_ref=table)
    inbox.complete("evt-1", [], table_ref=table)
    assert table.items["evt-1"]["status"] == inbox.UNMATCHED

    inbox.complete("evt-1", ["wf-a", "wf-b"], table_ref=table)
    assert table.items["evt-1"]["status"] == inbox.MATCHED
    assert table.items["evt-1"]["matched"] == ["wf-a", "wf-b"]

    inbox.complete("evt-1", None, error="exploded", table_ref=table)
    assert table.items["evt-1"]["status"] == inbox.FAILED
    assert table.items["evt-1"]["error"] == "exploded"


def test_complete_survives_storage_failures(table, monkeypatch):
    inbox.record(EVENT, table_ref=table)

    def boom(**kwargs):
        raise RuntimeError("dynamodb down")

    monkeypatch.setattr(table, "update_item", boom)
    assert inbox.complete("evt-1", ["wf"], table_ref=table) == inbox.MATCHED


def test_api_list_sorts_newest_first_and_filters_by_connector(table):
    inbox.record({**EVENT, "id": "evt-1", "connector": "webhook"}, table_ref=table)
    inbox.record({**EVENT, "id": "evt-2", "connector": "telegram"}, table_ref=table)
    inbox.record({**EVENT, "id": "evt-3", "connector": "webhook"}, table_ref=table)

    status, payload = inbox.api_list(table_ref=table)
    assert status == 200
    assert [event["inbox_id"] for event in payload["events"]] == ["evt-3", "evt-2", "evt-1"]

    status, payload = inbox.api_list("telegram", table_ref=table)
    assert [event["inbox_id"] for event in payload["events"]] == ["evt-2"]


def seed_event(table, inbox_id, received_at, connector="webhook"):
    """A row with a controlled received_at, so paging order is deterministic."""
    table.items[inbox_id] = {
        "inbox_id": inbox_id,
        "connector": connector,
        "event": "hook",
        "source": "github",
        "received_at": received_at,
        "status": inbox.UNMATCHED,
        "matched": [],
        "data": {},
        "expires_at": 0,
    }


def test_api_list_pages_through_every_event_without_overlap(table):
    """More events than one page: the token walks the whole set, and the
    walk ends with paging.next None — nothing past the first page is lost."""
    for index in range(60):
        seed_event(table, f"evt-{index:03d}",
                   f"2026-09-27T10:00:{index:02d}+00:00")

    status, first = inbox.api_list(table_ref=table)
    assert status == 200
    assert len(first["events"]) == 25
    assert first["total"] == 60
    assert first["paging"]["limit"] == 25
    assert first["paging"]["filtered"] is False

    seen = [event["inbox_id"] for event in first["events"]]
    token = first["paging"]["next"]
    assert token
    while token:
        status, page = inbox.api_list(next_token=token, table_ref=table)
        assert status == 200
        seen.extend(event["inbox_id"] for event in page["events"])
        token = page["paging"]["next"]

    assert len(seen) == len(set(seen))  # pages never overlap
    assert len(seen) == 60  # every seeded event is reachable
    assert seen == sorted(seen, reverse=True)  # newest first across pages
    assert token is None


def test_api_list_filter_and_token_compose(table):
    """The connector filter and the page token compose: continuation pages
    keep the filter and start strictly after the previous page."""
    for index in range(20):
        connector = "webhook" if index % 2 else "telegram"
        seed_event(table, f"evt-{index:03d}",
                   f"2026-09-27T10:00:{index:02d}+00:00", connector=connector)

    status, first = inbox.api_list("webhook", limit=4, table_ref=table)
    assert status == 200
    assert [event["connector"] for event in first["events"]] == ["webhook"] * 4
    assert first["paging"]["filtered"] is True
    token = first["paging"]["next"]
    assert token

    seen = [event["inbox_id"] for event in first["events"]]
    while token:
        status, page = inbox.api_list("webhook", limit=4, next_token=token,
                                      table_ref=table)
        assert status == 200
        assert len(page["events"]) <= 4
        assert all(event["connector"] == "webhook" for event in page["events"])
        seen.extend(event["inbox_id"] for event in page["events"])
        token = page["paging"]["next"]

    assert len(seen) == len(set(seen)) == 10  # only the webhook events, once each


def test_api_list_rejects_an_invalid_token(table):
    status, payload = inbox.api_list(next_token="garbage", table_ref=table)
    assert status == 400
    assert payload == {"error": "Invalid page token"}


def test_api_list_orders_received_at_ties_by_inbox_id(table):
    """The inbox-id tiebreak makes same-second events order stably, which is
    what the page token's strict-after comparison relies on."""
    for inbox_id in ("evt-b", "evt-a", "evt-c"):
        seed_event(table, inbox_id, "2026-09-27T10:00:00+00:00")
    status, payload = inbox.api_list(table_ref=table)
    assert [event["inbox_id"] for event in payload["events"]] == \
        ["evt-c", "evt-b", "evt-a"]


def test_api_list_pages_stop_at_the_bounded_window(table):
    """The documented trade: the scan window stays bounded, so events older
    than it age out of the list (each stays reachable through api_get). The
    fake serves rows in insertion order, so the window holds the oldest 150
    here and the walk never reads the remaining rows."""
    for index in range(200):
        seed_event(table, f"evt-{index:03d}",
                   f"2026-09-27T10:{index // 60:02d}:{index % 60:02d}+00:00")

    status, first = inbox.api_list(table_ref=table)
    seen = [event["inbox_id"] for event in first["events"]]
    token = first["paging"]["next"]
    while token:
        _, page = inbox.api_list(next_token=token, table_ref=table)
        seen.extend(event["inbox_id"] for event in page["events"])
        token = page["paging"]["next"]

    assert len(seen) == 150  # the scan window, not the whole table
    assert len(seen) == len(set(seen))


def test_api_get_returns_the_stored_envelope(table):
    inbox.record(EVENT, table_ref=table)
    status, payload = inbox.api_get("evt-1", table_ref=table)
    assert status == 200
    assert payload["event"]["data"] == {"action": "opened"}

    status, payload = inbox.api_get("missing", table_ref=table)
    assert status == 404


def test_api_replay_sends_a_fresh_event_tied_to_the_original(table, monkeypatch):
    inbox.record(EVENT, table_ref=table)
    queue = FakeQueue()
    monkeypatch.setenv("EVENT_QUEUE_URL", "https://queue")

    status, payload = inbox.api_replay("evt-1", queue=queue, table_ref=table)

    assert status == 202
    assert payload["accepted"] is True
    assert payload["replayed_from"] == "evt-1"
    sent = json.loads(queue.messages[0][1])
    assert sent["id"] != "evt-1"
    assert sent["correlation_id"] == "evt-1"
    assert sent["connector"] == "webhook"
    assert sent["data"] == {"action": "opened"}


def test_api_replay_rejects_truncated_data(table):
    table.items["evt-big"] = dict(EVENT, inbox_id="evt-big",
                                  data={"truncated": True, "preview": "..."})
    status, payload = inbox.api_replay("evt-big", queue=FakeQueue(), table_ref=table)
    assert status == 409


def test_record_stores_20kb_data_and_replays_it(table, monkeypatch):
    """The stored-data cap matches the raised run-history trigger input, so
    an ordinary large webhook event stays whole in the inbox and replays."""
    big = {"body": "x" * 20_000}
    assert inbox.record({**EVENT, "data": big}, table_ref=table) == "evt-1"
    assert table.items["evt-1"]["data"] == big

    monkeypatch.setenv("EVENT_QUEUE_URL", "https://queue")
    status, _payload = inbox.api_replay("evt-1", queue=FakeQueue(), table_ref=table)
    assert status == 202


def test_record_previews_data_past_the_replay_cap(table):
    inbox.record({**EVENT, "data": {"body": "x" * (inbox.DATA_LIMIT + 1)}},
                 table_ref=table)
    stored = table.items["evt-1"]["data"]
    assert stored["truncated"] is True
    assert len(stored["preview"]) == inbox.DATA_LIMIT


def test_api_replay_404_for_unknown_events(table):
    status, payload = inbox.api_replay("missing", queue=FakeQueue(), table_ref=table)
    assert status == 404
