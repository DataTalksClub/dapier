"""Digest actions: collect items across runs, release them as one flush.

add/flush round trip in arrival order, dedupe, the pending-item cap, JSON
items keeping their shape, TTL, per-workflow scoping, and the registry
entries. Same fake-table approach as test_storage_actions.py — moto's
laxer expression handling would hide real expression mistakes the live
service rejects.
"""
import boto3
import pytest
import time

from src.dapier.connectors import registry
from src.dapier.engine.actions import digest


EVENT = {
    "id": "evt-1",
    "connector": "email",
    "event": "message.received",
    "data": {"text": "todo buy milk"},
}

STEPS = {"lookup": {"status": "completed", "output": {"row": 7}}}


class FakeStorageTable:
    """scope/key-keyed table honoring the conditions kv_find relies on."""

    def __init__(self):
        self.items = {}

    def put_item(self, Item):
        self.items[(Item["scope"], Item["key"])] = dict(Item)

    def get_item(self, Key):
        item = self.items.get((Key["scope"], Key["key"]))
        return {"Item": dict(item)} if item else {}

    def delete_item(self, Key, ReturnValues=None):
        item = self.items.pop((Key["scope"], Key["key"]), None)
        return {"Attributes": dict(item)} if item and ReturnValues == "ALL_OLD" else {}

    def query(self, **kwargs):
        expression = kwargs["KeyConditionExpression"]
        leaves = expression._values if type(expression).__name__ == "And" else (expression,)
        matches = [
            dict(item) for (scope, key), item in sorted(self.items.items())
            if all(self._leaf_ok(leaf, scope, key) for leaf in leaves)
        ]
        if kwargs.get("Select") == "COUNT":
            return {"Count": len(matches)}
        limit = kwargs.get("Limit")
        return {"Items": matches[:limit] if limit else matches}

    @staticmethod
    def _leaf_ok(leaf, scope, key):
        name, expected = leaf._values[0].name, leaf._values[1]
        actual = scope if name == "scope" else key
        if type(leaf).__name__ == "BeginsWith":
            return actual.startswith(expected)
        return actual == expected


@pytest.fixture()
def storage_table(monkeypatch):
    table = FakeStorageTable()
    monkeypatch.setenv("STORAGE_TABLE", "workflow-state")

    class Dynamo:
        def Table(self, _name):
            return table

    monkeypatch.setattr(boto3, "resource", lambda service: Dynamo())
    return table


def run(runner, action, workflow_id="wf-1", steps=None):
    return getattr(digest, runner)(action, EVENT, workflow_id, steps=steps)


def add(key, item=None, **extra):
    action = {"type": "digest_add", "key": key, **extra}
    if item is not None:
        action["item"] = item
    return run("run_digest_add", action)


def flush(key):
    return run("run_digest_flush", {"type": "digest_flush", "key": key})


def test_add_then_flush_roundtrips_in_arrival_order(storage_table):
    assert add("todos", "first") == {"key": "todos", "added": True, "count": 1}
    assert add("todos", "second") == {"key": "todos", "added": True, "count": 2}

    released = flush("todos")

    assert released == {"key": "todos", "items": ["first", "second"],
                        "count": 2, "empty": False}
    assert not storage_table.items


def test_flush_empties_and_a_second_flush_is_empty(storage_table):
    add("todos", "only")
    flush("todos")

    assert flush("todos") == {"key": "todos", "items": [], "count": 0, "empty": True}


def test_dedupe_skips_repeat_items(storage_table):
    assert add("seen", "same@example.com", dedupe=True)["added"] is True
    assert add("seen", "same@example.com", dedupe=True) == {
        "key": "seen", "added": False, "count": 1}

    assert flush("seen")["items"] == ["same@example.com"]


def test_without_dedupe_repeats_are_kept(storage_table):
    add("notes", "same")
    assert add("notes", "same")["added"] is True
    assert flush("notes")["count"] == 2


def test_dedupe_designer_strings_are_understood(storage_table):
    add("seen", "x", dedupe="true")
    assert add("seen", "x", dedupe="true")["added"] is False


def test_max_items_drops_the_oldest(storage_table):
    add("todos", "a", max_items=2)
    add("todos", "b", max_items=2)

    assert add("todos", "c", max_items=2) == {
        "key": "todos", "added": True, "count": 2}

    assert flush("todos")["items"] == ["b", "c"]


def test_default_cap_drops_the_oldest_too(storage_table, monkeypatch):
    monkeypatch.setattr(digest, "DIGEST_DEFAULT_MAX_ITEMS", 2)
    add("todos", "a")
    add("todos", "b")

    assert add("todos", "c")["count"] == 2

    assert flush("todos")["items"] == ["b", "c"]


def test_max_items_must_be_a_positive_whole_number(storage_table):
    with pytest.raises(ValueError, match="whole number"):
        add("todos", "x", max_items="soon")
    with pytest.raises(ValueError, match="at least 1"):
        add("todos", "x", max_items=0)


def test_flush_without_reset_peeks_and_keeps_items(storage_table):
    add("todos", "first")

    peeked = run("run_digest_flush",
                 {"type": "digest_flush", "key": "todos", "reset": False})
    assert peeked == {"key": "todos", "items": ["first"],
                      "count": 1, "empty": False}
    assert storage_table.items, "peek must keep the digest pending"

    assert flush("todos")["items"] == ["first"]
    assert not storage_table.items


def test_item_defaults_to_the_event_data(storage_table):
    add("events")

    assert flush("events")["items"] == [{"text": "todo buy milk"}]


def test_item_renders_templates_from_event_and_steps(storage_table):
    add("todos", "{text}")
    run("run_digest_add",
        {"type": "digest_add", "key": "todos", "item": "row {steps.lookup.output.row}"},
        steps=STEPS)

    assert flush("todos")["items"] == ["todo buy milk", "row 7"]


def test_json_items_keep_their_shape_after_the_flush(storage_table):
    add("rows", {"row": 7, "from": "{text}"})

    released = flush("rows")

    assert released["items"] == [{"row": 7, "from": "todo buy milk"}]


def test_plain_json_scalars_stay_strings(storage_table):
    add("rows", "7")

    assert flush("rows")["items"] == ["7"]


def test_add_with_ttl_stamps_expires(storage_table):
    add("todos", "fleeting", ttl_seconds=60)

    (stored,) = storage_table.items.values()
    assert stored["expires"] > time.time()


def test_digests_are_scoped_per_workflow(storage_table):
    run("run_digest_add", {"type": "digest_add", "key": "todos", "item": "mine"},
        workflow_id="wf-2")

    other = run("run_digest_flush", {"type": "digest_flush", "key": "todos"},
                workflow_id="wf-1")
    assert other == {"key": "todos", "items": [], "count": 0, "empty": True}

    own = run("run_digest_flush", {"type": "digest_flush", "key": "todos"},
              workflow_id="wf-2")
    assert own["items"] == ["mine"]


def test_missing_key_is_a_clear_error(storage_table):
    with pytest.raises(ValueError, match="requires a key"):
        run("run_digest_add", {"type": "digest_add", "item": "x"})
    with pytest.raises(ValueError, match="requires a key"):
        run("run_digest_flush", {"type": "digest_flush"})


def test_registry_has_the_two_digest_types():
    assert {"digest_add", "digest_flush"} <= set(registry.ACTIONS)
    assert registry.ACTIONS["digest_add"].required == frozenset({"key"})
    assert registry.ACTIONS["digest_add"].optional == frozenset(
        {"item", "dedupe", "max_items", "ttl_seconds"})
    assert registry.ACTIONS["digest_flush"].required == frozenset({"key"})
    assert registry.ACTIONS["digest_flush"].optional == frozenset({"reset"})


def test_registered_run_forwards_workflow_id_and_steps(storage_table):
    output = registry.ACTIONS["digest_add"].run(
        {"type": "digest_add", "key": "todos", "item": "row {steps.lookup.output.row}"},
        EVENT, "wf-9", steps=STEPS,
    )

    assert output == {"key": "todos", "added": True, "count": 1}
    ((scope, key), stored), = storage_table.items.items()
    assert scope == "wf-9" and key.startswith("digest/todos/")
    assert stored["value"] == "row 7"


def test_digest_chain_passes_save_validation():
    registry.validate_action_chain([
        {"type": "digest_add", "id": "collect", "key": "todos", "item": "{text}"},
        {"type": "digest_flush", "id": "release", "key": "todos"},
    ])


def test_save_validation_rejects_bad_digest_steps():
    with pytest.raises(registry.ActionError, match="unknown keys"):
        registry.validate_action_chain([
            {"type": "digest_add", "id": "collect", "key": "todos",
             "item": "{text}", "items": []},
        ])
    with pytest.raises(registry.ActionError, match="not a number"):
        registry.validate_action_chain([
            {"type": "digest_add", "id": "collect", "key": "todos",
             "item": "{text}", "ttl_seconds": "soon"},
        ])
    with pytest.raises(registry.ActionError, match="not a number"):
        registry.validate_action_chain([
            {"type": "digest_add", "id": "collect", "key": "todos",
             "item": "{text}", "max_items": "plenty"},
        ])
    with pytest.raises(registry.ActionError, match="boolean"):
        registry.validate_action_chain([
            {"type": "digest_flush", "id": "release", "key": "todos",
             "reset": "maybe"},
        ])
