"""api_list must show freshly written rows: the scan reads the whole
retention window (paginated, item-capped), never a single arbitrary page.

Oct-16 regression: handled email sat in the table while the list read one
Limit-capped scan page from an arbitrary slice of the hash-key space, so
new rows could stay invisible for days - the "silent drop" that was no
drop at all.
"""
import boto3
import pytest

from src.dapier.triggers import inbox
from tests.test_inbox import FakeTable, seed_event


class TruncatingTable(FakeTable):
    """Serve at most Limit rows per scan and signal the rest through
    LastEvaluatedKey, like the real service - the base fake returns the
    whole table every time, which would make pagination untestable."""

    def scan(self, **kwargs):
        items = [dict(item) for item in self.items.values()]
        start = kwargs.get("ExclusiveStartKey", {}).get("inbox_id")
        if start is not None:
            keys = list(self.items)
            items = items[keys.index(start) + 1:]
        limit = kwargs.get("Limit")
        page = items[:limit] if limit else items
        out = {"Items": page}
        if page and items[len(page):]:
            out["LastEvaluatedKey"] = {"inbox_id": page[-1]["inbox_id"]}
        return out


@pytest.fixture
def table(monkeypatch):
    """The truncating fake behind the boto3 resource shim."""
    fake = TruncatingTable()
    monkeypatch.setenv(inbox.TABLE_ENV, "trigger-inbox")

    class Dynamo:
        def Table(self, _name):
            return fake

    monkeypatch.setattr(boto3, "resource", lambda service: Dynamo())
    return fake


def test_api_list_shows_newest_rows_written_after_older_ones(table):
    """The globally newest row leads the very first page even when the table
    holds far more rows than the old single-page window ever read."""
    for index in range(500):
        seed_event(table, f"evt-{index:03d}",
                   f"2026-09-27T{index // 60:02d}:{index % 60:02d}:00+00:00")

    status, payload = inbox.api_list(table_ref=table)
    assert status == 200
    assert payload["events"][0]["inbox_id"] == "evt-499"
    seen = [event["inbox_id"] for event in payload["events"]]
    assert len(seen) == len(set(seen))


def test_list_walks_every_page_when_the_table_exceeds_one_page(table, monkeypatch):
    """Pages compose through LastEvaluatedKey: a page size far below the row
    count still surfaces the newest row and the full set while paging."""
    monkeypatch.setattr(inbox, "SCAN_PAGE", 10)
    for index in range(50):
        seed_event(table, f"evt-{index:03d}",
                   f"2026-09-27T10:{index // 60:02d}:{index % 60:02d}+00:00")

    status, payload = inbox.api_list(table_ref=table)
    assert status == 200
    assert payload["events"][0]["inbox_id"] == "evt-049"

    seen = [event["inbox_id"] for event in payload["events"]]
    token = payload["paging"]["next"]
    while token:
        _, page = inbox.api_list(next_token=token, table_ref=table)
        seen.extend(event["inbox_id"] for event in page["events"])
        token = page["paging"]["next"]
    assert sorted(seen) == sorted(f"evt-{index:03d}" for index in range(50))


def test_api_list_still_bounded_by_the_item_cap(table, monkeypatch):
    """The pagination stops at SCAN_WINDOW_ITEMS: a table past the cap reads
    a bounded prefix of pages instead of unbounded scanning. With the fake's
    deterministic insertion order, pages of 40 over a cap of 100 read three
    pages (120 rows), not the whole 150-row table."""
    monkeypatch.setattr(inbox, "SCAN_PAGE", 40)
    monkeypatch.setattr(inbox, "SCAN_WINDOW_ITEMS", 100)
    for index in range(150):
        seed_event(table, f"evt-{index:03d}",
                   f"2026-09-27T{index // 60:02d}:{index % 60:02d}:00+00:00")

    seen = []
    status, payload = inbox.api_list(table_ref=table)
    seen.extend(event["inbox_id"] for event in payload["events"])
    token = payload["paging"]["next"]
    while token:
        _, page = inbox.api_list(next_token=token, table_ref=table)
        seen.extend(event["inbox_id"] for event in page["events"])
        token = page["paging"]["next"]

    assert len(seen) == 120  # the item cap stopped the walk, not the table
    assert len(seen) == len(set(seen))
