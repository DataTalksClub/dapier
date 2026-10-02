"""Every trigger store must walk all scan pages, never stop at Limit=200.

The four stores (email, hook, schedule, poll) feed both their list APIs and
the engine's all_workflows() from load_items(); a single Limit=200 scan
silently dropped trigger #201 and beyond — the trigger kept its API entry
missing and, worse, stopped firing with no error anywhere. These tests page
the stub table exactly like the DynamoDB resource API does (Limit caps the
page, LastEvaluatedKey/ExclusiveStartKey continue the walk) and prove each
store loads its whole table without duplicates, and that the engine merge
sees trigger #201.
"""

import math
import os
import unittest
from unittest.mock import patch

from src.dapier.engine import all_workflows
from src.dapier.triggers import (
    email_triggers, hook_triggers, poll_triggers, schedule_triggers,
)


class PagedTable:
    """DynamoDB stand-in that pages scan() like the real resource API.

    Items beyond the first page are invisible to a caller that scans once —
    the old bug, reproduced faithfully. get/put/delete are the same shape the
    per-store test stubs use, so api_save/delete work against it too.
    """

    def __init__(self, items, key):
        self.items = {item[key]: dict(item) for item in items}
        self.key = key
        self.scans = 0

    def scan(self, Limit=200, ExclusiveStartKey=None, ConsistentRead=False):
        self.scans += 1
        keys = sorted(self.items)
        start = 0
        if ExclusiveStartKey is not None:
            start = keys.index(ExclusiveStartKey[self.key]) + 1
        page_keys = keys[start:start + Limit]
        page = {"Items": [dict(self.items[k]) for k in page_keys]}
        if start + Limit < len(keys):
            page["LastEvaluatedKey"] = {self.key: page_keys[-1]}
        return page

    def get_item(self, Key):
        item = self.items.get(Key[self.key])
        return {"Item": dict(item)} if item else {}

    def put_item(self, Item):
        self.items[Item[self.key]] = dict(Item)

    def delete_item(self, Key):
        self.items.pop(Key[self.key], None)


class SinglePageTable(PagedTable):
    """A table that always returns everything in one page (the shape the
    per-store test stubs and small real tables have) — the walk must cope."""

    def scan(self, Limit=200, ExclusiveStartKey=None, ConsistentRead=False):
        self.scans += 1
        return {"Items": [dict(item) for item in self.items.values()]}


ACTIONS = [{"type": "webhook", "url": "https://hooks.test/x"}]
COUNT = 230  # > SCAN_LIMIT (200): the old single scan stopped at item 200


def name_for(index):
    return f"trigger-{index:04d}"


class PagedTableSanityTests(unittest.TestCase):
    """The stub must genuinely truncate, or the walk tests prove nothing."""

    def test_stub_pages_like_dynamodb_and_hides_the_rest(self):
        table = PagedTable(
            [{"name": name_for(index)} for index in range(COUNT)], key="name")
        page = table.scan(Limit=email_triggers.SCAN_LIMIT)
        self.assertEqual(len(page["Items"]), email_triggers.SCAN_LIMIT)
        self.assertEqual([item["name"] for item in page["Items"]],
                         [name_for(index) for index in range(email_triggers.SCAN_LIMIT)])
        self.assertIn("LastEvaluatedKey", page)
        follow_up = table.scan(Limit=email_triggers.SCAN_LIMIT,
                               ExclusiveStartKey=page["LastEvaluatedKey"])
        self.assertEqual([item["name"] for item in follow_up["Items"]],
                         [name_for(index) for index in range(email_triggers.SCAN_LIMIT, COUNT)])
        self.assertNotIn("LastEvaluatedKey", follow_up)


class StoreWalkContract:
    """The shared walk contract; subclasses bind one trigger store.

    ``key`` is the stored item's identity attribute, ``env`` the TABLE_ENV
    name all_workflows() gates the store on, ``prefix`` the workflow id
    prefix the store's workflow_for produces (``email-trigger-<name>``...).
    """

    key = None
    env = None
    prefix = None

    def store(self):
        raise NotImplementedError

    def item(self, index):
        raise NotImplementedError

    def seeded(self):
        return PagedTable([self.item(index) for index in range(COUNT)], key=self.key)

    def test_load_items_walks_every_page_without_duplicates(self):
        store = self.store()
        table = self.seeded()
        loaded = store.load_items(table_ref=table)
        names = [item[self.key] for item in loaded]
        self.assertEqual(len(names), COUNT)
        self.assertEqual(len(set(names)), COUNT)
        self.assertEqual(names, sorted(names))
        self.assertGreaterEqual(table.scans, math.ceil(COUNT / store.SCAN_LIMIT))

    def test_load_workflows_walks_every_page(self):
        store = self.store()
        table = self.seeded()
        workflows = store.load_workflows(table_ref=table)
        self.assertEqual(len(workflows), COUNT)
        self.assertEqual(len({workflow["id"] for workflow in workflows}), COUNT)
        self.assertEqual(
            sorted(workflow["id"] for workflow in workflows),
            sorted(f"{self.prefix}-trigger-{name_for(index)}" for index in range(COUNT)))

    def test_engine_all_workflows_sees_trigger_201(self):
        # The engine merge reads the same load_items per invocation: with the
        # old single 200-item scan, trigger #201 silently stopped firing.
        store = self.store()
        table = self.seeded()
        missing_id = f"{self.prefix}-trigger-{name_for(200)}"
        with patch.dict(os.environ, {self.env: "triggers"}), \
                patch(f"{store.__name__}.get_table", return_value=table):
            merged = all_workflows()
        ids = [workflow["id"] for workflow in merged]
        self.assertEqual(len(ids), COUNT)
        self.assertIn(missing_id, ids)

    def test_single_page_table_still_loads(self):
        store = self.store()
        table = SinglePageTable([self.item(index) for index in range(5)], key=self.key)
        loaded = store.load_items(table_ref=table)
        self.assertEqual([item[self.key] for item in loaded],
                         [name_for(index) for index in range(5)])
        self.assertEqual(table.scans, 1)


class EmailStoreWalkTests(StoreWalkContract, unittest.TestCase):
    key = "name"
    env = email_triggers.TABLE_ENV
    prefix = "email"

    def store(self):
        return email_triggers

    def item(self, index):
        return {"name": name_for(index), "enabled": True, "actions": [dict(ACTIONS[0])]}


class HookStoreWalkTests(StoreWalkContract, unittest.TestCase):
    key = "hook_id"
    env = hook_triggers.TABLE_ENV
    prefix = "webhook"

    def store(self):
        return hook_triggers

    def item(self, index):
        return {"hook_id": name_for(index), "kind": "webhook", "enabled": True,
                "actions": [dict(ACTIONS[0])]}


class ScheduleStoreWalkTests(StoreWalkContract, unittest.TestCase):
    key = "schedule_id"
    env = schedule_triggers.TABLE_ENV
    prefix = "schedule"

    def store(self):
        return schedule_triggers

    def item(self, index):
        return {"schedule_id": name_for(index), "enabled": True,
                "actions": [dict(ACTIONS[0])]}


class PollStoreWalkTests(StoreWalkContract, unittest.TestCase):
    key = "poll_id"
    env = poll_triggers.TABLE_ENV
    prefix = "poll"

    def store(self):
        return poll_triggers

    def item(self, index):
        return {"poll_id": name_for(index), "enabled": True,
                "actions": [dict(ACTIONS[0])]}


if __name__ == "__main__":
    unittest.main()
