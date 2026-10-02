"""The bookkeeping review queue: staging, dedup, confirm, reject.

Like the other DynamoDB-backed tests, the table is a fake honoring exactly
the access shapes the store uses (conditional puts, expression scans) —
not moto, whose laxer expression handling would hide real mistakes.
"""
import pytest

from src.dapier import bookkeeping_store


class ConditionalCheckFailed(Exception):
    pass


class _Exceptions:
    ConditionalCheckFailedException = ConditionalCheckFailed


class _Client:
    exceptions = _Exceptions()


class _Meta:
    client = _Client()


class FakeBookkeepingTable:
    """pk/sk-keyed table with the store's access shapes."""

    def __init__(self):
        self.items = {}
        self.meta = _Meta()

    def put_item(self, Item, ConditionExpression=None):
        key = (Item["pk"], Item["sk"])
        if ConditionExpression and key in self.items:
            raise ConditionalCheckFailed()
        self.items[key] = dict(Item)

    def get_item(self, Key):
        item = self.items.get((Key["pk"], Key["sk"]))
        return {"Item": dict(item)} if item else {}

    def delete_item(self, Key):
        self.items.pop((Key["pk"], Key["sk"]), None)

    def update_item(self, Key, UpdateExpression, ExpressionAttributeNames,
                    ExpressionAttributeValues):
        item = self.items[(Key["pk"], Key["sk"])]
        sets = UpdateExpression.split("SET", 1)[1]
        for assignment in sets.split(","):
            target, value = assignment.split("=", 1)
            target, value = target.strip(), value.strip()
            if value.startswith(":"):
                value = ExpressionAttributeValues[value]
            elif value in ExpressionAttributeNames:
                value = item[ExpressionAttributeNames[value]]
            if target.startswith("#"):
                target = ExpressionAttributeNames[target]
            item[target] = value

    def scan(self, FilterExpression=None, ExpressionAttributeNames=None,
             ExpressionAttributeValues=None, Select=None, Limit=None,
             ExclusiveStartKey=None):
        names = ExpressionAttributeNames or {}
        values = ExpressionAttributeValues or {}
        start = ExclusiveStartKey and (ExclusiveStartKey["pk"], ExclusiveStartKey["sk"])
        matches = []
        for key in sorted(self.items):
            if start and key <= start:
                continue
            item = self.items[key]
            if self._filter_ok(item, FilterExpression, names, values):
                matches.append(dict(item))
        page = matches[:Limit] if Limit else matches
        result = {"Items": page, "Count": len(page)}
        if len(matches) > len(page):
            result["LastEvaluatedKey"] = {"pk": page[-1]["pk"], "sk": page[-1]["sk"]}
        return result

    @staticmethod
    def _filter_ok(item, expression, names, values):
        # The store's filters are `#a = :a [AND #b = :b]` — name-to-value
        # equality only, which is all the fake has to honor.
        if not expression:
            return True
        for clause in expression.split(" AND "):
            left, right = (part.strip() for part in clause.split("=", 1))
            attr = names.get(left, left)
            expected = values.get(right, right)
            if item.get(attr) != expected:
                return False
        return True


@pytest.fixture
def table():
    return FakeBookkeepingTable()


def stage(table, **overrides):
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(bookkeeping_store, "_table", lambda: table)
        entry = {"provider": "AWS", "invoice_number": "EUINDE26-1",
                 "what": "Cloud services", "amount": 70.57, "currency": "USD",
                 "vat": 11.27, "period": "September 1 - September 30, 2026",
                 "period_month": "2026-09", "account": "experiments"}
        return bookkeeping_store.create_pending(
            entry=overrides.pop("entry", entry),
            source=overrides.pop("source", "deterministic"),
            workflow_id=overrides.pop("workflow_id", "invoice-intake"),
            message=overrides.pop("message", {"message_id": "<m-1>",
                                              "subject": "AWS invoice"}),
            pdf=overrides.pop("pdf", {"filename": "invoice.pdf"}),
            **overrides)


def test_create_pending_stages_a_review_entry(table):
    result = stage(table)
    assert result["status"] == "pending"
    assert result["deduped"] is False
    queued = bookkeeping_store.list_entries(table=table)
    assert len(queued) == 1
    entry = queued[0]
    assert entry["entry"]["amount"] == 70.57  # Decimal roundtrip back to float
    assert entry["entry"]["vat"] == 11.27
    assert entry["source"] == "deterministic"
    assert entry["message"]["message_id"] == "<m-1>"
    assert entry["pdf"]["filename"] == "invoice.pdf"


def test_redelivery_dedupes_into_the_first_entry(table):
    first = stage(table)
    second = stage(table)
    assert second["entry_id"] == first["entry_id"]
    assert second["deduped"] is True
    assert len(bookkeeping_store.list_entries(table=table)) == 1


def test_different_invoice_numbers_do_not_dedupe(table):
    stage(table)
    stage(table, entry={"provider": "AWS", "invoice_number": "EUINDE26-2",
                        "amount": 1.0})
    assert len(bookkeeping_store.list_entries(table=table)) == 2


def test_rejected_invoice_can_be_restaged(table):
    first = stage(table)
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(bookkeeping_store, "_table", lambda: table)
        bookkeeping_store.reject_entry(first["entry_id"])
    again = stage(table)
    assert again["deduped"] is False
    assert again["entry_id"] != first["entry_id"]
    statuses = {e["entry_id"]: e["status"]
                for e in bookkeeping_store.list_entries(table=table)}
    assert statuses[again["entry_id"]] == "pending"


def test_confirm_applies_reviewer_edits(table):
    result = stage(table)
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(bookkeeping_store, "_table", lambda: table)
        confirmed = bookkeeping_store.confirm_entry(
            result["entry_id"], edits={"amount": 691.13, "account": None},
            confirmed_by="alexey")
    assert confirmed["status"] == "confirmed"
    assert confirmed["entry"]["amount"] == 691.13
    assert "account" not in confirmed["entry"]
    assert confirmed["confirmed_by"] == "alexey"


def test_confirm_rejects_unknown_edit_fields(table):
    result = stage(table)
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(bookkeeping_store, "_table", lambda: table)
        with pytest.raises(bookkeeping_store.BookkeepingError):
            bookkeeping_store.confirm_entry(result["entry_id"],
                                            edits={"workflow_id": "x"})


def test_only_pending_entries_can_be_confirmed_or_rejected(table):
    result = stage(table)
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(bookkeeping_store, "_table", lambda: table)
        bookkeeping_store.confirm_entry(result["entry_id"])
        with pytest.raises(bookkeeping_store.BookkeepingError):
            bookkeeping_store.reject_entry(result["entry_id"])


def test_status_filter_and_counts(table):
    first = stage(table)
    stage(table, entry={"provider": "OpenAI", "invoice_number": "X-2",
                        "amount": 23.8})
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(bookkeeping_store, "_table", lambda: table)
        bookkeeping_store.confirm_entry(first["entry_id"])
        pending = bookkeeping_store.list_entries(status="pending", table=table)
        assert len(pending) == 1
        assert pending[0]["entry"]["provider"] == "OpenAI"
        tally = bookkeeping_store.counts(table=table)
    assert tally == {"pending": 1, "confirmed": 1, "rejected": 0, "total": 2}


def test_dedup_marker_without_entry_is_ignored(table):
    stage(table)
    # Simulate a crashed stage: the marker survived, the entry did not.
    marker = next(key for key in table.items if key[0] == "dedup")
    entry_id = table.items[marker]["entry_id"]
    del table.items[("entry", entry_id)]
    result = stage(table)
    assert result["deduped"] is False
    assert result["entry_id"] != entry_id
