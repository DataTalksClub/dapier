"""Seen store domain behavior: claim-once ids in a TTL-bounded per-scope set."""
import pytest
from botocore.exceptions import ClientError

from src.dapier.triggers import seen


class FakeTable:
    """The DynamoDB semantics seen.py relies on: full-item put, an upsert
    update that sets one map key in place (condition honoured), delete.
    Items are keyed by whichever hash attribute they carry (scope_id for
    seen rows, cursor_id when shared with the poll cursors in tests)."""

    def __init__(self):
        self.items = {}

    @staticmethod
    def _hash(Key):
        return next(iter(Key.values()))

    def get_item(self, Key):
        item = self.items.get(self._hash(Key))
        return {"Item": dict(item)} if item else {}

    def put_item(self, Item, **kwargs):
        self.items[self._hash(Item)] = dict(Item)

    def update_item(self, Key, UpdateExpression=None, ConditionExpression=None,
                    ExpressionAttributeNames=None, ExpressionAttributeValues=None, **kwargs):
        # seen.claim's shape: SET seen.#k = :expires, expires_at = :expires,
        # updated_at = :at with attribute_not_exists(#m.#k).
        names, values = ExpressionAttributeNames or {}, ExpressionAttributeValues or {}
        if UpdateExpression == "SET #m = if_not_exists(#m, :empty)":
            scope = self._hash(Key)
            item = self.items.setdefault(scope, dict(Key))
            item.setdefault(names["#m"], dict(values[":empty"]))
            return {}
        scope, map_name, key_name = self._hash(Key), names["#m"], names["#k"]
        item = self.items.get(scope) or {}
        if key_name in (item.get(map_name) or {}):
            raise ClientError({"Error": {"Code": "ConditionalCheckFailedException"}},
                              "UpdateItem")
        item = dict(item)
        item["scope_id"] = scope
        item.setdefault(map_name, {})[key_name] = values[":expires"]
        item["expires_at"] = values[":expires"]
        item["updated_at"] = values[":at"]
        self.items[scope] = item
        return {}

    def delete_item(self, Key):
        self.items.pop(self._hash(Key), None)


@pytest.fixture
def table():
    return FakeTable()


def test_claim_is_true_once_per_key(table):
    assert seen.claim("hook#orders", "orders-abc", table_ref=table) is True
    assert seen.claim("hook#orders", "orders-abc", table_ref=table) is False


def test_claim_keeps_sibling_entries(table):
    """A second claim on the same scope must extend the seen map, not
    replace it — the first delivery's id has to stay claimed."""
    assert seen.claim("hook#orders", "orders-1", table_ref=table) is True
    assert seen.claim("hook#orders", "orders-2", table_ref=table) is True

    live = seen.load("hook#orders", table_ref=table)
    assert set(live) == {"orders-1", "orders-2"}


def test_expired_entry_claims_again(table):
    now = 1_800_000_000
    assert seen.claim("poll#daily", "daily-1", table_ref=table, now=now) is True
    # Same key, past its TTL: the claim re-reads, sees the expiry, retakes it.
    assert seen.claim("poll#daily", "daily-1", table_ref=table,
                      now=now + 31 * 86400) is True
    live = seen.load("poll#daily", table_ref=table, now=now + 31 * 86400)
    assert set(live) == {"daily-1"}


def test_prune_drops_expired_then_keeps_the_newest_cap():
    now = 1_000
    entries = {f"k{i}": now + i for i in range(5)}
    entries["old"] = now - 1

    pruned = seen.prune(entries, now=now, max_entries=3)

    assert "old" not in pruned
    assert sorted(pruned) == ["k2", "k3", "k4"]


def test_remember_merges_over_a_concurrent_write(table):
    first = seen.remember("poll#daily", "daily-1", table_ref=table)
    # A concurrent writer merges the same snapshot and adds its own id...
    seen.remember("poll#daily", "daily-2", table_ref=table, entries=first)
    # ...then the stale caller records too: nobody's id may be lost.
    seen.remember("poll#daily", "daily-3", table_ref=table, entries=first)

    assert set(seen.load("poll#daily", table_ref=table)) == {"daily-1", "daily-2", "daily-3"}


def test_forget_releases_the_key_and_empties_the_scope(table):
    assert seen.claim("hook#orders", "orders-1", table_ref=table) is True

    seen.forget("hook#orders", "orders-1", table_ref=table)

    assert "hook#orders" not in table.items
    assert seen.claim("hook#orders", "orders-1", table_ref=table) is True


def test_forget_keeps_sibling_entries(table):
    seen.claim("hook#orders", "orders-1", table_ref=table)
    seen.claim("hook#orders", "orders-2", table_ref=table)

    seen.forget("hook#orders", "orders-1", table_ref=table)

    assert set(seen.load("hook#orders", table_ref=table)) == {"orders-2"}


def test_unconfigured_store_raises(table, monkeypatch):
    monkeypatch.delenv("CURSORS_TABLE", raising=False)
    with pytest.raises(seen.StoreError):
        seen.claim("hook#orders", "orders-1")


def test_fresh_scope_claims_use_real_dynamodb_nested_map_semantics():
    import boto3
    from moto import mock_aws

    with mock_aws():
        resource = boto3.resource("dynamodb", region_name="eu-west-1")
        table = resource.create_table(
            TableName="test-seen-cursors",
            KeySchema=[{"AttributeName": "cursor_id", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "cursor_id", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        assert seen.claim("hook#fresh", "first", table_ref=table) is True
        assert seen.claim("hook#fresh", "first", table_ref=table) is False
        assert seen.claim("hook#fresh", "second", table_ref=table) is True
        assert seen.claim("hook#fresh", "first", table_ref=table) is False
        assert set(seen.load("hook#fresh", table_ref=table)) == {"first", "second"}
