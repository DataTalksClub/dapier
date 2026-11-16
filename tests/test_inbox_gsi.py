"""GSI-backed inbox listing: connector queries use by-connector-received."""
import boto3
import pytest
from moto import mock_aws

from src.dapier.triggers import inbox


@pytest.fixture
def gsi_table():
    with mock_aws():
        dynamodb = boto3.resource("dynamodb", region_name="eu-west-1")
        table = dynamodb.create_table(
            TableName="trigger-inbox",
            AttributeDefinitions=[
                {"AttributeName": "inbox_id", "AttributeType": "S"},
                {"AttributeName": "connector", "AttributeType": "S"},
                {"AttributeName": "received_at", "AttributeType": "S"},
            ],
            KeySchema=[{"AttributeName": "inbox_id", "KeyType": "HASH"}],
            GlobalSecondaryIndexes=[
                {
                    "IndexName": inbox.CONNECTOR_RECEIVED_INDEX,
                    "KeySchema": [
                        {"AttributeName": "connector", "KeyType": "HASH"},
                        {"AttributeName": "received_at", "KeyType": "RANGE"},
                    ],
                    "Projection": {"ProjectionType": "ALL"},
                }
            ],
            BillingMode="PAY_PER_REQUEST",
        )
        yield table


def _put(table, inbox_id, connector, received_at, status=inbox.MATCHED, data=None):
    table.put_item(
        Item={
            "inbox_id": inbox_id,
            "connector": connector,
            "event": "message.received" if connector == "email" else "hook",
            "source": "ses" if connector == "email" else "github",
            "received_at": received_at,
            "status": status,
            "matched": ["wf-1"] if status == inbox.MATCHED else [],
            "data": data or {},
            "expires_at": 9999999999,
        }
    )


def test_connector_list_queries_gsi_without_scanning(gsi_table, monkeypatch):
    for index in range(120):
        _put(
            gsi_table,
            f"mail-{index:03d}",
            "email",
            f"2026-11-16T10:{index // 60:02d}:{index % 60:02d}+00:00",
        )
    for index in range(50):
        _put(
            gsi_table,
            f"hook-{index:03d}",
            "webhook",
            f"2026-11-16T11:00:{index % 60:02d}+00:00",
        )

    monkeypatch.setattr(
        gsi_table,
        "scan",
        lambda **_kwargs: (_ for _ in ()).throw(AssertionError("scan must not run")),
    )

    status, first = inbox.api_list("email", limit=100, table_ref=gsi_table)
    assert status == 200
    assert len(first["events"]) == 100
    assert first["events"][0]["inbox_id"] == "mail-119"
    assert first["events"][-1]["inbox_id"] == "mail-020"
    assert first["paging"]["next"]

    status, second = inbox.api_list(
        "email", limit=100, next_token=first["paging"]["next"], table_ref=gsi_table
    )
    assert status == 200
    assert len(second["events"]) == 20
    assert second["events"][0]["inbox_id"] == "mail-019"
    assert second["events"][-1]["inbox_id"] == "mail-000"
    assert second["paging"]["next"] is None


def test_gsi_pagination_handles_same_second_ties(gsi_table):
    for suffix in ("a", "b", "c", "d", "e"):
        _put(gsi_table, f"mail-{suffix}", "email", "2026-11-16T10:00:00+00:00")
    _put(gsi_table, "mail-older", "email", "2026-11-16T09:59:59+00:00")

    seen = []
    token = None
    while True:
        status, page = inbox.api_list("email", limit=2, next_token=token, table_ref=gsi_table)
        assert status == 200
        seen.extend(event["inbox_id"] for event in page["events"])
        token = page["paging"]["next"]
        if not token:
            break

    assert seen == ["mail-e", "mail-d", "mail-c", "mail-b", "mail-a", "mail-older"]


def test_gsi_filters_by_outcome_and_multi_connector_hook(gsi_table):
    _put(gsi_table, "mail-1", "email", "2026-11-16T10:00:01+00:00", status=inbox.IGNORED)
    _put(gsi_table, "mail-2", "email", "2026-11-16T10:00:02+00:00", status=inbox.MATCHED)
    _put(gsi_table, "mail-3", "email", "2026-11-16T10:00:03+00:00", status=inbox.FAILED)
    _put(gsi_table, "wh-1", "webhook", "2026-11-16T10:00:04+00:00", data={"hook": "orders"})
    _put(gsi_table, "tg-1", "telegram", "2026-11-16T10:00:05+00:00", data={"hook": "orders"})
    _put(gsi_table, "wh-2", "webhook", "2026-11-16T10:00:06+00:00", data={"hook": "other"})

    status, refused = inbox.api_list("email", outcome="refused", table_ref=gsi_table)
    assert status == 200
    assert [event["inbox_id"] for event in refused["events"]] == ["mail-1"]

    status, multi = inbox.api_list(
        ("webhook", "telegram"), hook="orders", table_ref=gsi_table
    )
    assert status == 200
    assert [event["inbox_id"] for event in multi["events"]] == ["tg-1", "wh-1"]


def test_record_without_connector_succeeds_with_gsi(gsi_table):
    inbox_id = inbox.record(
        {"id": "evt-noconn", "connector": None, "event": "ping", "data": {}},
        table_ref=gsi_table,
    )
    assert inbox_id == "evt-noconn"
    stored = gsi_table.get_item(Key={"inbox_id": "evt-noconn"})["Item"]
    assert "connector" not in stored
