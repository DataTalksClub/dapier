"""Storage actions: per-workflow key-value state that survives runs.

set/get roundtrip, overwrite, missing-key and delete semantics, prefix
finds, TTL, template rendering, and the registry entries. Like the other
DynamoDB-backed tests (test_usage.py, test_device_login.py), the table is a
fake with boto3.resource stubbed — moto's laxer expression handling would
hide real expression mistakes the live service rejects.
"""
import time

import boto3
import pytest

from src.dapier.connectors import registry
from src.dapier.engine.actions import storage


EVENT = {
    "id": "evt-1",
    "connector": "email",
    "event": "message.received",
    "data": {"text": "todo buy milk"},
}

STEPS = {"lookup": {"status": "completed", "output": {"row": 7}}}


class FakeStorageTable:
    """scope/key-keyed table honoring the conditions storage_find relies on."""

    def __init__(self):
        self.items = {}

    def put_item(self, Item):
        self.items[(Item["scope"], Item["key"])] = dict(Item)

    def get_item(self, Key):
        item = self.items.get((Key["scope"], Key["key"]))
        return {"Item": dict(item)} if item else {}

    def delete_item(self, Key, ReturnValues=None):
        item = self.items.pop((Key["scope"], Key["key"]), None)
        # Real DynamoDB only reports Attributes when ALL_OLD had something.
        return {"Attributes": dict(item)} if item and ReturnValues == "ALL_OLD" else {}

    def query(self, **kwargs):
        expression = kwargs["KeyConditionExpression"]
        leaves = expression._values if type(expression).__name__ == "And" else (expression,)
        matches = [
            dict(item) for (scope, key), item in sorted(self.items.items())
            if all(self._leaf_ok(leaf, scope, key) for leaf in leaves)
        ]
        limit = kwargs.get("Limit")
        return {"Items": matches[:limit] if limit else matches}

    @staticmethod
    def _leaf_ok(leaf, scope, key):
        name, expected = leaf._values[0].name, leaf._values[1]
        actual = scope if name == "scope" else key
        if type(leaf).__name__ == "BeginsWith":
            # The live service rejects an empty key-condition value
            # (ValidationException); fail the same way here.
            if expected == "":
                raise ValueError("The AttributeValue for a key attribute cannot contain an empty string value")
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
    return getattr(storage, runner)(action, EVENT, workflow_id, steps=steps)


def test_set_then_get_roundtrips_the_value(storage_table):
    output = run("run_storage_set", {"type": "storage_set", "key": "cursor", "value": "inbox/42"})

    assert output == {"key": "cursor", "stored": True}
    stored = storage_table.items[("wf-1", "cursor")]
    assert stored["value"] == "inbox/42"
    assert stored["updated_at"]  # ISO timestamp rides every write

    assert run("run_storage_get", {"type": "storage_get", "key": "cursor"}) == {
        "key": "cursor", "value": "inbox/42", "found": True,
    }


def test_set_overwrites_an_existing_key(storage_table):
    run("run_storage_set", {"type": "storage_set", "key": "cursor", "value": "first"})
    run("run_storage_set", {"type": "storage_set", "key": "cursor", "value": "second"})

    assert storage_table.items[("wf-1", "cursor")]["value"] == "second"


def test_get_missing_key_reports_not_found(storage_table):
    assert run("run_storage_get", {"type": "storage_get", "key": "nope"}) == {
        "key": "nope", "value": "", "found": False,
    }


def test_delete_reports_whether_the_key_was_there(storage_table):
    run("run_storage_set", {"type": "storage_set", "key": "cursor", "value": "v"})

    assert run("run_storage_delete", {"type": "storage_delete", "key": "cursor"}) == {
        "key": "cursor", "deleted": True,
    }
    assert run("run_storage_get", {"type": "storage_get", "key": "cursor"})["found"] is False
    # Deleting a missing key is fine, not an error.
    assert run("run_storage_delete", {"type": "storage_delete", "key": "cursor"}) == {
        "key": "cursor", "deleted": False,
    }


def test_find_lists_keys_under_the_prefix_ascending(storage_table):
    for key in ("counter:sms", "other", "counter:email"):
        run("run_storage_set", {"type": "storage_set", "key": key, "value": f"v:{key}"})

    output = run("run_storage_find", {"type": "storage_find", "prefix": "counter:"})

    assert output == {
        "items": [{"key": "counter:email", "value": "v:counter:email"},
                  {"key": "counter:sms", "value": "v:counter:sms"}],
        "count": 2,
    }


def test_find_with_an_empty_prefix_lists_every_key_in_the_scope(storage_table):
    for key in ("b", "a", "counter:sms"):
        run("run_storage_set", {"type": "storage_set", "key": key, "value": key})
    storage.kv_set("other-wf", "a", "elsewhere")

    items = storage.kv_find("wf-1", "")

    assert [item["key"] for item in items] == ["a", "b", "counter:sms"]
    assert [item["key"] for item in storage.kv_find("wf-1", None)] == ["a", "b", "counter:sms"]


def test_find_defaults_to_20_and_caps_at_50(storage_table):
    for index in range(55):
        run("run_storage_set", {"type": "storage_set", "key": f"k:{index:02d}", "value": "v"})

    assert run("run_storage_find", {"type": "storage_find", "prefix": "k:"})["count"] == 20
    assert run("run_storage_find", {"type": "storage_find", "prefix": "k:", "limit": 100})["count"] == 50
    assert run("run_storage_find", {"type": "storage_find", "prefix": "k:", "limit": 3})["count"] == 3


def test_set_with_ttl_stamps_expires(storage_table):
    output = run("run_storage_set",
                 {"type": "storage_set", "key": "cursor", "value": "v", "ttl_seconds": "600"})

    expires = storage_table.items[("wf-1", "cursor")]["expires"]
    assert output["expires"] == expires
    assert expires == pytest.approx(time.time() + 600, abs=5)


def test_set_without_ttl_omits_expires(storage_table):
    output = run("run_storage_set", {"type": "storage_set", "key": "cursor", "value": "v"})

    assert "expires" not in output
    assert "expires" not in storage_table.items[("wf-1", "cursor")]


def test_set_value_renders_templates_from_event_and_steps(storage_table):
    run("run_storage_set", {"type": "storage_set", "key": "from-event", "value": "{text}"})
    run("run_storage_set", {"type": "storage_set", "key": "from-step",
                            "value": "row {steps.lookup.output.row}"}, steps=STEPS)

    assert storage_table.items[("wf-1", "from-event")]["value"] == "todo buy milk"
    assert storage_table.items[("wf-1", "from-step")]["value"] == "row 7"


def test_unset_storage_table_is_a_clear_error(storage_table, monkeypatch):
    monkeypatch.delenv("STORAGE_TABLE")

    with pytest.raises(ValueError, match="STORAGE_TABLE"):
        run("run_storage_get", {"type": "storage_get", "key": "cursor"})


def test_run_without_a_workflow_id_is_a_clear_error(storage_table):
    with pytest.raises(ValueError, match="workflow id"):
        run("run_storage_set", {"type": "storage_set", "key": "cursor", "value": "v"},
            workflow_id="")


def test_registry_has_the_four_storage_types():
    assert {"storage_get", "storage_set", "storage_delete", "storage_find"} <= set(registry.ACTIONS)
    assert registry.ACTIONS["storage_set"].required == frozenset({"key", "value"})
    assert registry.ACTIONS["storage_set"].optional == frozenset({"ttl_seconds"})
    assert registry.ACTIONS["storage_find"].required == frozenset({"prefix"})
    assert registry.ACTIONS["storage_find"].optional == frozenset({"limit"})


def test_registered_run_forwards_workflow_id_and_steps(storage_table):
    output = registry.ACTIONS["storage_set"].run(
        {"type": "storage_set", "key": "row", "value": "{steps.lookup.output.row}"},
        EVENT, "wf-9", steps=STEPS,
    )

    assert output == {"key": "row", "stored": True}
    stored = storage_table.items[("wf-9", "row")]
    assert stored["scope"] == "wf-9"
    assert stored["value"] == "7"


def test_storage_chain_passes_save_validation():
    registry.validate_action_chain([
        {"type": "storage_set", "id": "remember", "key": "last-row", "value": "{text}"},
        {"type": "storage_get", "id": "recall", "key": "last-row"},
    ])


def test_save_validation_rejects_bad_storage_steps():
    with pytest.raises(registry.ActionError, match="unknown keys"):
        registry.validate_action_chain([
            {"type": "storage_get", "id": "recall", "key": "k", "scope": "wf-1"},
        ])
    with pytest.raises(registry.ActionError, match="not a number"):
        registry.validate_action_chain([
            {"type": "storage_set", "id": "remember", "key": "k", "value": "v",
             "ttl_seconds": "soon"},
        ])


if __name__ == "__main__":
    pytest.main([__file__])
