"""Double-run dedupe for triggers (gap-analysis finding 7).

Two holes, one store. A ``next_cursor`` poll re-lists items across fires
(providers recycle pages, and an emitted item can surface again on a later
page), so each emitted item needs an "already published" marker beside the
cursor — the seen-set in ``triggers.seen``. A webhook trigger with a
``dedupe_path`` claims the delivery's stable id at the ingress, so a
provider retry answers 202 without a second run. Time is injected through
``triggers.seen._now``, which makes the TTL window directly testable.
"""
import unittest
from unittest.mock import patch

from botocore.exceptions import ClientError

from src.dapier.api import router as ingress
from src.dapier.triggers import hook_triggers, poll_triggers, seen
from src.dapier.triggers.email_triggers import TriggerError

T0 = 1_000_000
DAY = 86400


def conditional_failure():
    return ClientError({"Error": {"Code": "ConditionalCheckFailedException",
                                  "Message": "the conditional request failed"}},
                       "PutItem")


class FakeStateTable:
    """DynamoDB stand-in for the cursors table (cursor_id hash key) with the
    conditional update the seen store's claim decides on: it fails exactly
    when the scope's map already holds the claimed key."""

    def __init__(self):
        self.items = {}

    def get_item(self, Key):
        item = self.items.get(Key["cursor_id"])
        return {"Item": dict(item)} if item else {}

    def put_item(self, Item, **_kwargs):
        self.items[Item["cursor_id"]] = dict(Item)

    def update_item(self, Key, UpdateExpression=None, ConditionExpression=None,
                    ExpressionAttributeNames=None, ExpressionAttributeValues=None,
                    **_kwargs):
        key = Key["cursor_id"]
        names = ExpressionAttributeNames or {}
        values = ExpressionAttributeValues or {}
        if UpdateExpression == "SET #m = if_not_exists(#m, :empty)":
            entry = self.items.setdefault(key, dict(Key))
            entry.setdefault(names["#m"], dict(values[":empty"]))
            return {}
        map_attr = names.get("#m", "seen")
        claimed = names.get("#k")
        entry = dict(self.items.get(key) or {})
        entries = dict(entry.get(map_attr) or {})
        if ConditionExpression and claimed in entries:
            raise conditional_failure()
        entries[claimed] = values[":expires"]
        entry[map_attr] = entries
        entry[names.get("#e", "expires_at")] = values[":expires"]
        entry[names.get("#u", "updated_at")] = values[":at"]
        self.items[key] = entry

    def delete_item(self, Key):
        self.items.pop(Key["cursor_id"], None)


# --- Poll triggers: the seen-set beside the cursor ---------------------------

def next_cursor_poll(**overrides):
    poll = {"poll_id": "news", "enabled": True, "cursor_mode": "next_cursor",
            "id_path": "", "cursor_path": "meta.next", "cursor_query": "page",
            "max_items": 25, "dedupe_ttl_days": seen.TTL_DAYS,
            "url": "https://example.test/list", "method": "GET", "headers": {}}
    poll.update(overrides)
    return poll


class NextCursorSeenSetTests(unittest.TestCase):
    """fire(): an item already emitted must not re-run when its page — or a
    later one — lists it again inside the dedupe window."""

    def run_fire(self, poll, *, stored_cursor, items, next_cursor="page-2",
                 cursors=None, fail_on=None, now=T0):
        fired_events = []
        parked = []
        cursors = cursors or FakeStateTable()

        def fake_execute(event, **_kwargs):
            if fail_on is not None and event["data"]["item_id"] == fail_on:
                raise RuntimeError("downstream exploded")
            fired_events.append(event["data"]["item_id"])

        with patch.object(poll_triggers, "get_item", return_value=poll), \
             patch.object(poll_triggers, "get_cursor", return_value=stored_cursor), \
             patch.object(poll_triggers, "fetch_page_response",
                          return_value=(list(items), next_cursor)), \
             patch.object(poll_triggers, "put_cursor",
                          side_effect=lambda name, value, table=None: parked.append(value)), \
             patch("src.dapier.engine.execute", side_effect=fake_execute), \
             patch("src.dapier.engine.notify.notify_failure"), \
             patch("src.dapier.triggers.seen._now", return_value=now):
            result = poll_triggers.fire(poll["poll_id"], cursor_table_ref=cursors)

        return result, fired_events, parked, cursors

    def test_item_repeated_on_a_later_page_is_skipped(self):
        _r, fired, _p, cursors = self.run_fire(
            next_cursor_poll(), stored_cursor=None, items=[{"id": "row-1"}])
        self.assertEqual(fired, ["row-1"])

        result, fired_again, _p, _c = self.run_fire(
            next_cursor_poll(), stored_cursor="page-2",
            items=[{"id": "row-1"}, {"id": "row-3"}], cursors=cursors)

        # row-1 was published by the first fire; only row-3 runs now.
        self.assertEqual(fired_again, ["row-3"])
        self.assertEqual(result,
                         {"poll": "news", "fired": 1, "skipped_seen": 1})

    def test_a_recycled_page_fires_nothing_and_still_parks(self):
        _r, _f, _p, cursors = self.run_fire(
            next_cursor_poll(), stored_cursor=None,
            items=[{"id": "row-1"}, {"id": "row-2"}])

        result, fired_again, parked, _c = self.run_fire(
            next_cursor_poll(), stored_cursor=None,
            items=[{"id": "row-1"}, {"id": "row-2"}], cursors=cursors)

        self.assertEqual(fired_again, [])
        self.assertEqual(result,
                         {"poll": "news", "fired": 0, "skipped_seen": 2})
        self.assertEqual(parked, ["page-2"])

    def test_ttl_expiry_re_allows_the_item(self):
        _r, _f, _p, cursors = self.run_fire(
            next_cursor_poll(), stored_cursor=None, items=[{"id": "row-1"}])

        # One minute before the window closes the item still stays quiet.
        result, fired, _p, _c = self.run_fire(
            next_cursor_poll(), stored_cursor=None, items=[{"id": "row-1"}],
            cursors=cursors, now=T0 + seen.TTL_DAYS * DAY - 60)
        self.assertEqual(fired, [])
        self.assertEqual(result["skipped_seen"], 1)

        # Past the window the same item is deliverable again.
        result, fired, _p, _c = self.run_fire(
            next_cursor_poll(), stored_cursor=None, items=[{"id": "row-1"}],
            cursors=cursors, now=T0 + seen.TTL_DAYS * DAY + 60)
        self.assertEqual(fired, ["row-1"])
        self.assertEqual(result, {"poll": "news", "fired": 1})

    def test_dedupe_ttl_days_override_shortens_the_window(self):
        poll = next_cursor_poll(dedupe_ttl_days=1)
        _r, _f, _p, cursors = self.run_fire(poll, stored_cursor=None,
                                            items=[{"id": "row-1"}])
        event_id = poll_triggers._stable_event_id("news", "row-1")
        seen_row = cursors.items["seen#poll#news"]
        self.assertEqual(seen_row["seen"][event_id], T0 + 1 * DAY)

        # Two days later a one-day window has already let the item go.
        result, fired, _p, _c = self.run_fire(poll, stored_cursor=None,
                                              items=[{"id": "row-1"}],
                                              cursors=cursors, now=T0 + 2 * DAY)
        self.assertEqual(fired, ["row-1"])
        self.assertEqual(result, {"poll": "news", "fired": 1})

    def test_failed_item_is_not_marked_seen_and_retries(self):
        cursors = FakeStateTable()
        _r, fired, parked, _c = self.run_fire(
            next_cursor_poll(), stored_cursor=None,
            items=[{"id": "row-1"}, {"id": "row-2"}],
            cursors=cursors, fail_on="row-2")
        self.assertEqual(fired, ["row-1"])
        self.assertEqual(parked, [])  # the page waits for a clean pass

        result, fired_again, parked_again, _c = self.run_fire(
            next_cursor_poll(), stored_cursor=None,
            items=[{"id": "row-1"}, {"id": "row-2"}],
            cursors=cursors)

        # row-1 stays quiet (already published); row-2 gets its retry.
        self.assertEqual(fired_again, ["row-2"])
        self.assertEqual(result,
                         {"poll": "news", "fired": 1, "skipped_seen": 1})
        self.assertEqual(parked_again, ["page-2"])

    def test_watermark_polls_do_not_touch_the_seen_set(self):
        poll = next_cursor_poll(cursor_mode="watermark", id_path="createdTime")
        with patch.object(poll_triggers, "get_item", return_value=poll), \
             patch.object(poll_triggers, "get_cursor", return_value=None), \
             patch.object(poll_triggers, "fetch_page_response",
                          return_value=([{"id": "row-1", "createdTime": "2"}], None)), \
             patch("src.dapier.engine.execute"), \
             patch("src.dapier.triggers.seen.load") as seen_load:
            result = poll_triggers.fire("news", cursor_table_ref=FakeStateTable())

        self.assertEqual(result, {"poll": "news", "fired": 1})
        seen_load.assert_not_called()  # the watermark governs; no seen-set


class PollDedupeConfigTests(unittest.TestCase):
    """build_item: ``dedupe_ttl_days`` defaults to the store's window and
    validates as a bounded number."""

    def poll_body(self, **overrides):
        body = {"name": "news", "expression": "rate(1 hour)",
                "url": "https://example.test/list",
                "cursor_mode": "next_cursor", "cursor_path": "meta.next",
                "actions": [{"type": "slack", "channel": "#news", "text": "n"}]}
        body.update(overrides)
        return body

    def test_dedupe_ttl_days_defaults_to_the_store_window(self):
        item = poll_triggers.build_item(self.poll_body(), "op@example.test")
        self.assertEqual(item["dedupe_ttl_days"], seen.TTL_DAYS)

    def test_dedupe_ttl_days_override_is_stored(self):
        item = poll_triggers.build_item(self.poll_body(dedupe_ttl_days=30),
                                        "op@example.test")
        self.assertEqual(item["dedupe_ttl_days"], 30)

    def test_dedupe_ttl_days_must_be_a_bounded_number(self):
        for bad in (0, 366, "soon"):
            with self.assertRaises(TriggerError):
                poll_triggers.build_item(self.poll_body(dedupe_ttl_days=bad),
                                         "op@example.test")


# --- Hook triggers: dedupe_path claims at the ingress ------------------------

def hook_item(**overrides):
    item = {"hook_id": "orders", "kind": "webhook", "dedupe_path": "event.id",
            "token": "tok", "actions": [], "enabled": True}
    item.update(overrides)
    return item


def claim_delivery(item, payload, *, cursors=None, now=T0):
    cursors = cursors or FakeStateTable()
    with patch("src.dapier.triggers.seen.get_table", return_value=cursors), \
         patch("src.dapier.triggers.seen._now", return_value=now):
        event_id, fresh = ingress._claim_delivery(item, payload)
    return event_id, fresh, cursors


class HookDedupeClaimTests(unittest.TestCase):
    """router._claim_delivery: a provider retry of the same dedupe value is
    skipped; anything that cannot be identified runs exactly as before."""

    def test_retry_with_the_same_dedupe_value_is_skipped(self):
        payload = {"event": {"id": "d1"}}
        first_id, first_fresh, cursors = claim_delivery(hook_item(), payload)
        self.assertTrue(first_fresh)
        self.assertEqual(first_id, hook_triggers.dedupe_event_id("orders", "d1"))

        second_id, second_fresh, _c = claim_delivery(
            hook_item(), payload, cursors=cursors)

        self.assertFalse(second_fresh)  # the retry is a duplicate
        self.assertEqual(second_id, first_id)

    def test_a_different_value_runs(self):
        _e, _f, cursors = claim_delivery(hook_item(), {"event": {"id": "d1"}})

        event_id, fresh, _c = claim_delivery(
            hook_item(), {"event": {"id": "d2"}}, cursors=cursors)

        self.assertTrue(fresh)
        self.assertEqual(event_id, hook_triggers.dedupe_event_id("orders", "d2"))

    def test_absent_dedupe_path_behaves_exactly_as_before(self):
        for payload in ({"event": {"id": "d1"}}, {"unrelated": True}):
            event_id, fresh, _c = claim_delivery(
                hook_item(dedupe_path=""), payload)
            self.assertEqual((event_id, fresh), (None, True))

    def test_missing_or_empty_value_at_the_path_runs_normally(self):
        for payload in ({"other": {"id": "d1"}}, {"event": {}},
                        {"event": {"id": ""}}):
            event_id, fresh, _c = claim_delivery(hook_item(), payload)
            self.assertEqual((event_id, fresh), (None, True))

    def test_ttl_expiry_re_allows_the_value(self):
        _e, _f, cursors = claim_delivery(hook_item(), {"event": {"id": "d1"}},
                                         now=T0)

        event_id, fresh, _c = claim_delivery(
            hook_item(), {"event": {"id": "d1"}}, cursors=cursors,
            now=T0 + seen.TTL_DAYS * DAY + 60)

        self.assertTrue(fresh)  # past the window the delivery runs again
        self.assertEqual(event_id, hook_triggers.dedupe_event_id("orders", "d1"))

    def test_a_store_outage_never_drops_a_delivery(self):
        with patch("src.dapier.triggers.seen.get_table",
                   side_effect=Exception("cursors table unavailable")):
            event_id, fresh = ingress._claim_delivery(
                hook_item(), {"event": {"id": "d1"}})

        self.assertTrue(fresh)  # publish anyway, with the stable id
        self.assertEqual(event_id, hook_triggers.dedupe_event_id("orders", "d1"))


class HookDedupeConfigTests(unittest.TestCase):
    """build_item: ``dedupe_path`` validates as a dotted payload path."""

    def hook_body(self, **overrides):
        body = {"name": "orders",
                "actions": [{"type": "webhook", "url": "https://x.test"}]}
        body.update(overrides)
        return body

    def test_dedupe_path_defaults_to_unset(self):
        item, _created = hook_triggers.build_item(self.hook_body(), "op", "webhook")
        self.assertEqual(item["dedupe_path"], "")

    def test_dedupe_path_is_stored(self):
        item, _created = hook_triggers.build_item(
            self.hook_body(dedupe_path="event.id"), "op", "webhook")
        self.assertEqual(item["dedupe_path"], "event.id")

    def test_dedupe_path_must_be_a_dotted_path(self):
        for bad in ("a..b", "!", 5):
            with self.assertRaises(TriggerError):
                hook_triggers.build_item(self.hook_body(dedupe_path=bad),
                                         "op", "webhook")


if __name__ == "__main__":
    unittest.main()
