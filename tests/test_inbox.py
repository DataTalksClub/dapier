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
    """put_item honours the dedupe condition; scan reads what was stored."""

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
        return {"Items": [dict(item) for item in self.items.values()]}


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


def test_api_replay_404_for_unknown_events(table):
    status, payload = inbox.api_replay("missing", queue=FakeQueue(), table_ref=table)
    assert status == 404
