"""Poll fire × seen-store interaction: failures stay unseen so they retry,
and watermark fires never touch the seen store."""
import unittest
from unittest.mock import patch

from src.dapier.triggers import poll_triggers


class FakeCursorTable:
    """DynamoDB stand-in keyed by cursor_id (poll cursors) or scope_id
    (the seen store's per-trigger sets, triggers.seen)."""

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


def next_cursor_item(**overrides):
    item = {"poll_id": "inbox-watch", "enabled": True,
            "cursor_mode": "next_cursor", "id_path": "", "list_path": "",
            "cursor_path": "meta.next", "cursor_query": "page",
            "max_items": 25, "url": "https://example.test/list",
            "method": "GET", "headers": {}}
    item.update(overrides)
    return item


class SeenStoreInteractionTests(unittest.TestCase):
    def run_fire(self, item, page, cursors, fail_on=None):
        fired_events = []

        def fake_execute(event, **_kwargs):
            if fail_on is not None and event["data"]["item_id"] == fail_on:
                raise RuntimeError("downstream exploded")
            fired_events.append(event["data"]["item_id"])

        with patch.object(poll_triggers, "get_item", return_value=item), \
             patch.object(poll_triggers, "get_cursor", return_value=None), \
             patch.object(poll_triggers, "fetch_page_response",
                          return_value=(list(page), "page-2")), \
             patch.object(poll_triggers, "put_cursor"), \
             patch("src.dapier.engine.execute", side_effect=fake_execute), \
             patch("src.dapier.engine.notify.notify_failure"):
            result = poll_triggers.fire(item["poll_id"], cursor_table_ref=cursors)

        return result, fired_events

    def test_a_failed_item_is_not_marked_seen(self):
        """A failed run must retry on the next fire: only the item whose run
        succeeded enters the seen set."""
        cursors = FakeCursorTable()
        page = [{"id": "row-1"}, {"id": "row-2"}]

        first, fired_first = self.run_fire(next_cursor_item(), page, cursors,
                                           fail_on="row-2")
        self.assertEqual(fired_first, ["row-1"])
        seen_row = cursors.items["seen#poll#inbox-watch"]
        self.assertEqual(len(seen_row["seen"]), 1)

        second, fired_second = self.run_fire(next_cursor_item(), page, cursors)

        self.assertEqual(fired_second, ["row-2"])  # row-1 seen-skipped, row-2 retried
        self.assertEqual(second["fired"], 1)

    def test_watermark_fires_never_touch_the_seen_store(self):
        item = next_cursor_item(cursor_mode="watermark", id_path="createdTime")
        page = [{"id": "a", "createdTime": "2026-09-26T21:00:00.000Z"},
                {"id": "b", "createdTime": "2026-09-26T22:00:00.000Z"}]

        result, fired = self.run_fire(item, page, FakeCursorTable())

        self.assertEqual(result["fired"], 2)
        self.assertEqual(fired, ["2026-09-26T21:00:00.000Z", "2026-09-26T22:00:00.000Z"])

    def test_next_cursor_fire_keeps_only_seen_rows_beside_the_cursor(self):
        """The seen scope is named for its trigger, so two polls never share
        a dedupe set."""
        cursors = FakeCursorTable()

        self.run_fire(next_cursor_item(), [{"id": "row-1"}], cursors)
        self.run_fire(next_cursor_item(poll_id="other"), [{"id": "row-1"}], cursors)

        self.assertIn("seen#poll#inbox-watch", cursors.items)
        self.assertIn("seen#poll#other", cursors.items)
        self.assertNotEqual(
            cursors.items["seen#poll#inbox-watch"]["seen"],
            cursors.items["seen#poll#other"]["seen"])
