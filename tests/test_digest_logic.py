"""Digest / batching logic step: accumulate across runs, flush as one batch.

The engine-level half of G11 (the ``digest`` logic step; the registry-side
half lives in tests/test_digest.py alongside the ``digest_add``/
``digest_flush`` actions). ``mode: accumulate`` appends rendered item(s) to
a per-workflow batch (``engine.actions.digests``, the DIGESTS_TABLE) and a
later run — usually the nightly schedule fire — ``mode: flush`` claims the
whole batch at once. The claim is one atomic ``delete_item(ALL_OLD)``, so a
second flush (a redelivery, or a concurrent run) sees an empty digest
instead of double-sending; the claimed items ride the event as ``digest``
so following steps template ``{digest.items}`` / ``{digest.count}``. An
empty flush records ``skipped`` and the chain runs on.

Fake-table approach as test_storage_actions.py — and the fake interprets
the exact UpdateExpression the module emits (list_append/if_not_exists), so
a drifted expression fails loudly instead of silently "working" (moto's
laxer expression handling would hide that).
"""
import re

import boto3
import pytest

from src.dapier.engine import logic
from src.dapier.engine.actions import digests, templating


EVENT = {
    "id": "evt-1",
    "connector": "email",
    "event": "message.received",
    "data": {"subject": "Invoice 42", "route": "invoice"},
}

ACCUMULATE = {"id": "collect", "type": "digest", "key": "nightly",
              "item": "{subject}"}
FLUSH = {"id": "send-digest", "type": "digest", "mode": "flush", "key": "nightly"}


class Hooks:
    """Record the telemetry calls the chain makes, like the worker's ledger."""

    def __init__(self, pending=True):
        self.calls = []
        self.pending = pending

    def before(self, workflow_id, action_id, event, action_type):
        self.calls.append(("before", action_id, action_type))
        return self.pending

    def after(self, workflow_id, action_id, event, **kwargs):
        self.calls.append(("after", action_id, kwargs.get("status"), kwargs.get("output")))

    def error(self, workflow_id, action_id, event, exc, **kwargs):
        self.calls.append(("error", action_id, str(exc)))


class FakeDigestsTable:
    """scope/key-keyed table honoring the expressions digests.py emits."""

    UPDATE_RE = re.compile(
        r"^SET (?P<list>#\w+) = list_append\(if_not_exists\((?P=list), :(?P<empty>\w+)\), "
        r":(?P<batch>\w+)\), (?P<stamp>#\w+) = :(?P<now>\w+)$")

    def __init__(self):
        self.items = {}

    def update_item(self, Key, UpdateExpression, ExpressionAttributeNames,
                    ExpressionAttributeValues, ReturnValues=None):
        match = self.UPDATE_RE.match(UpdateExpression.strip())
        assert match, f"fake table cannot parse UpdateExpression: {UpdateExpression!r}"
        names = ExpressionAttributeNames
        values = ExpressionAttributeValues
        list_attr = names[match.group("list")]
        empty = values[f":{match.group('empty')}"]
        batch = values[f":{match.group('batch')}"]
        stamp_attr = names[match.group("stamp")]
        now = values[f":{match.group('now')}"]
        assert empty == [], "if_not_exists seed must be an empty list"
        assert isinstance(batch, list)

        key = (Key["scope"], Key["key"])
        item = self.items.get(key, {})
        current = item.get(list_attr) if isinstance(item.get(list_attr), list) else []
        item = {**item, list_attr: current + list(batch), stamp_attr: now}
        self.items[key] = item
        if ReturnValues == "UPDATED_NEW":
            return {"Attributes": {list_attr: item[list_attr]}}
        return {}

    def get_item(self, Key):
        item = self.items.get((Key["scope"], Key["key"]))
        return {"Item": dict(item)} if item else {}

    def delete_item(self, Key, ReturnValues=None):
        item = self.items.pop((Key["scope"], Key["key"]), None)
        # Real DynamoDB only reports Attributes when ALL_OLD had something.
        return {"Attributes": dict(item)} if item and ReturnValues == "ALL_OLD" else {}


@pytest.fixture()
def digest_table(monkeypatch):
    table = FakeDigestsTable()
    monkeypatch.setenv("DIGESTS_TABLE", "digests")

    class Dynamo:
        def Table(self, _name):
            return table

    monkeypatch.setattr(boto3, "resource", lambda service: Dynamo())
    return table


def run_chain(steps, *, workflow_id="wf-1", data=None, hooks=None, run_action=None):
    hooks = hooks or Hooks()
    event = {**EVENT, "data": dict(EVENT["data"] if data is None else data)}
    stop = logic.run_chain(
        workflow_id, steps, event,
        run_action or (lambda action, event, workflow_id, steps=None: {"ok": True}),
        before_action=hooks.before, after_action=hooks.after,
        on_action_error=hooks.error,
    )
    return stop, hooks, event


class TestAccumulate:
    """mode accumulate (the default): append and report the new total."""

    def test_accumulate_appends_the_rendered_item_and_reports_the_count(self, digest_table):
        stop, hooks, _event = run_chain([ACCUMULATE])

        assert stop is None
        assert digest_table.items[("wf-1", "nightly")]["items"] == ["Invoice 42"]
        assert ("after", "collect", "completed", {"key": "nightly", "digested": 1}) \
            in hooks.calls

    def test_the_count_grows_across_runs(self, digest_table):
        run_chain([ACCUMULATE])
        stop, hooks, _event = run_chain(
            [ACCUMULATE], data={"subject": "Invoice 43"})

        assert stop is None
        assert digest_table.items[("wf-1", "nightly")]["items"] == [
            "Invoice 42", "Invoice 43"]
        assert ("after", "collect", "completed", {"key": "nightly", "digested": 2}) \
            in hooks.calls

    def test_item_templates_can_reference_earlier_steps(self, digest_table):
        step = {"id": "collect", "type": "digest", "key": "nightly",
                "item": "{steps.render.output.url}"}
        event = {**EVENT, "data": dict(EVENT["data"])}
        hooks = Hooks()

        def render_then_collect(action, evt, workflow_id, steps=None):
            if action["id"] == "render":
                return {"url": "https://files.test/invoice-42.pdf"}
            return {}

        logic.run_chain(
            "wf-1",
            [{"id": "render", "type": "webhook", "url": "https://files.test"}, step],
            event, render_then_collect,
            before_action=hooks.before, after_action=hooks.after,
            on_action_error=hooks.error,
        )

        assert digest_table.items[("wf-1", "nightly")]["items"] == [
            "https://files.test/invoice-42.pdf"]

    def test_items_appends_every_entry_in_order_after_item(self, digest_table):
        step = {"id": "collect", "type": "digest", "key": "nightly",
                "item": "{subject}",
                "items": ["{route}", "fixed line"]}
        run_chain([step])

        assert digest_table.items[("wf-1", "nightly")]["items"] == [
            "Invoice 42", "invoice", "fixed line"]

    def test_mode_defaults_to_accumulate(self, digest_table):
        step = {"id": "collect", "type": "digest", "key": "nightly",
                "item": "{subject}"}
        run_chain([step])

        assert digest_table.items[("wf-1", "nightly")]["items"] == ["Invoice 42"]


class TestFlush:
    """mode flush: claim-and-clear, expose {digest.items}/{digest.count}."""

    @staticmethod
    def rendered_runner(rendered):
        """A runner like the real templating runners: fields render against
        the event and the steps captured so far."""
        def run(action, event, workflow_id, steps=None):
            rendered.append(templating.render(action["url"], event, steps))
            return {}
        return run

    def test_flush_drains_the_batch_and_exposes_digest_to_following_steps(
            self, digest_table):
        run_chain([ACCUMULATE], data={"subject": "line one"})
        run_chain([ACCUMULATE], data={"subject": "line two"})

        rendered = []
        stop, hooks, event = run_chain(
            [FLUSH, {"id": "post", "type": "webhook",
                     "url": "digest {digest.count}: {digest.items}"}],
            run_action=self.rendered_runner(rendered),
        )

        assert stop is None
        assert rendered == ["digest 2: [\"line one\", \"line two\"]"]
        assert event["data"]["digest"] == {
            "items": ["line one", "line two"], "count": 2}
        assert ("after", "send-digest", "completed",
                {"key": "nightly", "items": ["line one", "line two"], "count": 2}) \
            in hooks.calls
        assert digest_table.items.get(("wf-1", "nightly")) is None

    def test_flush_feeds_a_following_for_each(self, digest_table):
        run_chain([ACCUMULATE], data={"subject": "line one"})
        run_chain([ACCUMULATE], data={"subject": "line two"})

        ran = []
        stop, _hooks, _event = run_chain(
            [FLUSH, {"id": "each", "type": "for_each", "list": "digest.items",
                     "actions": [{"id": "post", "type": "webhook",
                                  "url": "to: {item}"}]}],
            run_action=lambda action, event, workflow_id, steps=None:
                ran.append(action["url"]) or {},
        )

        assert stop is None
        assert ran == ["to: line one", "to: line two"]

    def test_flush_of_an_empty_digest_is_skipped_and_the_chain_runs_on(
            self, digest_table):
        rendered = []
        stop, hooks, event = run_chain(
            [FLUSH, {"id": "post", "type": "webhook", "url": "sent {digest.count}"}],
            run_action=self.rendered_runner(rendered),
        )

        assert stop is None  # not an error: the chain continues
        assert rendered == ["sent 0"]  # {digest.count} renders 0, not empty
        assert ("after", "send-digest", "skipped",
                {"key": "nightly", "items": [], "count": 0, "empty": True}) \
            in hooks.calls
        assert event["data"]["digest"] == {"items": [], "count": 0}

    def test_a_second_flush_sees_an_empty_digest(self, digest_table):
        run_chain([ACCUMULATE])
        run_chain([FLUSH])

        stop, hooks, _event = run_chain([FLUSH])

        assert stop is None
        assert ("after", "send-digest", "skipped",
                {"key": "nightly", "items": [], "count": 0, "empty": True}) \
            in hooks.calls

    def test_two_consecutive_flush_steps_never_double_send(self, digest_table):
        run_chain([ACCUMULATE])
        run_chain([ACCUMULATE])

        ran = []
        stop, _hooks, _event = run_chain(
            [FLUSH, FLUSH, {"id": "post", "type": "webhook", "url": "{digest.count}"}],
            run_action=lambda action, event, workflow_id, steps=None:
                ran.append(action["id"]) or {},
        )

        # The first flush drains; the redelivery-shaped second one records
        # skipped and the chain runs on — the batch is released once.
        assert stop is None
        assert ran == ["post"]
        assert digest_table.items.get(("wf-1", "nightly")) is None

    def test_claim_is_one_shot_at_the_state_layer(self, digest_table):
        digests.digests_append("wf-1", "nightly", ["a", "b"])

        assert digests.digests_claim("wf-1", "nightly") == ["a", "b"]
        assert digests.digests_claim("wf-1", "nightly") == []
        assert digests.digests_get("wf-1", "nightly") == []

    def test_an_append_racing_a_flush_lands_in_the_next_digest(self, digest_table):
        digests.digests_append("wf-1", "nightly", ["before"])
        claimed = digests.digests_claim("wf-1", "nightly")
        digests.digests_append("wf-1", "nightly", ["after"])

        assert claimed == ["before"]
        assert digests.digests_get("wf-1", "nightly") == ["after"]


class TestIsolation:
    """Digests are scoped per workflow: one workflow cannot read or flush
    another's batch, even on the same key."""

    def test_the_same_key_stays_separate_per_workflow(self, digest_table):
        run_chain([ACCUMULATE], workflow_id="wf-a")
        run_chain([ACCUMULATE], workflow_id="wf-b", data={"subject": "wf-b item"})

        assert digests.digests_get("wf-a", "nightly") == ["Invoice 42"]
        assert digests.digests_get("wf-b", "nightly") == ["wf-b item"]

    def test_a_flush_of_another_workflow_leaves_the_batch_alone(self, digest_table):
        run_chain([ACCUMULATE], workflow_id="wf-a")

        stop, hooks, _event = run_chain([FLUSH], workflow_id="wf-b")

        assert ("after", "send-digest", "skipped",
                {"key": "nightly", "items": [], "count": 0, "empty": True}) \
            in hooks.calls
        assert digests.digests_get("wf-a", "nightly") == ["Invoice 42"]


class TestSharedScope:
    """``shared: true`` puts the digest where any workflow can reach it:
    accumulate in the event's workflow, flush from the schedule-triggered
    one — two workflow ids, one key. Without the flag the batch stays
    private to its workflow (TestIsolation)."""

    def test_a_shared_digest_accumulates_in_one_workflow_and_flushes_from_another(
            self, digest_table):
        shared = {"id": "collect", "type": "digest", "key": "nightly",
                  "item": "{subject}", "shared": True}
        flush = {"id": "send-digest", "type": "digest", "mode": "flush",
                 "key": "nightly", "shared": True}

        run_chain([shared], workflow_id="wf-email")
        # The shared partition, not either workflow's:
        assert ("*shared*", "nightly") in digest_table.items

        stop, hooks, event = run_chain([flush], workflow_id="wf-nightly")

        assert stop is None
        assert ("after", "send-digest", "completed",
                {"key": "nightly", "items": ["Invoice 42"], "count": 1}) \
            in hooks.calls
        assert event["data"]["digest"] == {"items": ["Invoice 42"], "count": 1}

    def test_shared_and_private_keys_never_meet(self, digest_table):
        shared = {"id": "collect", "type": "digest", "key": "nightly",
                  "item": "{subject}", "shared": True}
        run_chain([shared], workflow_id="wf-a")
        run_chain([ACCUMULATE], workflow_id="wf-a")

        assert digests.digests_get(digests.SHARED_SCOPE, "nightly") == ["Invoice 42"]
        assert digests.digests_get("wf-a", "nightly") == ["Invoice 42"]

    def test_a_private_flush_cannot_drain_a_shared_batch(self, digest_table):
        shared = {"id": "collect", "type": "digest", "key": "nightly",
                  "item": "{subject}", "shared": True}
        run_chain([shared], workflow_id="wf-email")

        stop, hooks, _event = run_chain([FLUSH], workflow_id="wf-nightly")

        assert ("after", "send-digest", "skipped",
                {"key": "nightly", "items": [], "count": 0, "empty": True}) \
            in hooks.calls
        assert digests.digests_get(digests.SHARED_SCOPE, "nightly") == ["Invoice 42"]


class TestConfigErrors:
    """Config errors fail the step loudly, like the other logic steps."""

    def test_a_missing_key_is_rejected(self, digest_table):
        with pytest.raises(ValueError, match="requires a key"):
            run_chain([{"id": "collect", "type": "digest", "item": "{subject}"}])

    def test_an_unknown_mode_is_rejected(self, digest_table):
        with pytest.raises(ValueError, match="mode must be one of"):
            run_chain([{"id": "collect", "type": "digest", "key": "k",
                        "mode": "append"}])

    def test_accumulate_without_anything_to_append_is_rejected(self, digest_table):
        with pytest.raises(ValueError, match="needs an item or a non-empty items list"):
            run_chain([{"id": "collect", "type": "digest", "key": "nightly"}])

    def test_a_non_list_items_field_is_rejected(self, digest_table):
        with pytest.raises(ValueError, match="items must be a non-empty list"):
            run_chain([{"id": "collect", "type": "digest", "key": "nightly",
                        "items": "{subject}"}])

    def test_a_missing_workflow_scope_is_rejected(self, digest_table):
        with pytest.raises(ValueError, match="workflow id"):
            logic.run_chain("", [ACCUMULATE], dict(EVENT),
                            lambda action, event, workflow_id, steps=None: {})

    def test_an_absent_table_env_var_degrades_with_a_clear_error(self, monkeypatch):
        monkeypatch.delenv("DIGESTS_TABLE", raising=False)

        with pytest.raises(ValueError, match="DIGESTS_TABLE"):
            digests.digests_append("wf-1", "nightly", ["x"])
        with pytest.raises(ValueError, match="DIGESTS_TABLE"):
            digests.digests_claim("wf-1", "nightly")

    def test_the_state_layer_rejects_an_empty_batch(self, digest_table):
        with pytest.raises(ValueError, match="non-empty batch"):
            digests.digests_append("wf-1", "nightly", [])


class TestRegistryAndValidation:
    """The catalog entry and the save-time checks."""

    def test_digest_is_a_registered_logic_step(self):
        from src.dapier.connectors import registry

        entry = registry.LOGIC.get("digest")
        assert entry is not None
        assert [field["key"] for field in entry.fields] == \
            ["mode", "key", "item", "items", "shared"]
        catalog = registry.catalog()
        assert any(step["type"] == "digest" for step in catalog["actions"])

    def test_dry_run_supports_the_digest_step(self, digest_table):
        from src.dapier.engine import dryrun

        workflow = {
            "id": "wf-digest", "enabled": True,
            "trigger": {"connector": "schedule", "event": "tick"},
            "actions": [ACCUMULATE, FLUSH],
        }
        report = dryrun.dry_run(workflow, {"subject": "x"})
        by_id = {step["action_id"]: step for step in report["steps"]}
        assert by_id["collect"]["ok"] is True
        assert by_id["send-digest"]["ok"] is True

    def parse(self, actions_yaml):
        from src.dapier.api import designer_store

        return designer_store.parse_workflow(
            "id: wf-digest\n"
            "trigger: {connector: schedule, event: tick}\n"
            "actions:\n" + actions_yaml)

    def test_save_accepts_valid_digest_steps(self):
        workflow = self.parse(
            "  - {id: collect, type: digest, key: nightly, item: '{subject}'}\n"
            "  - {id: send, type: digest, mode: flush, key: nightly}\n")
        kinds = [action["type"] for action in workflow["actions"]]
        assert kinds == ["digest", "digest"]

    def test_save_accepts_an_items_list(self):
        workflow = self.parse(
            "  - {id: collect, type: digest, key: nightly,\n"
            "     items: ['{subject}', '{route}']}\n")
        assert workflow["actions"][0]["items"] == ["{subject}", "{route}"]

    @pytest.mark.parametrize("yaml_text,fragment", [
        ("  - {id: d, type: digest, key: k, mode: append, item: x}\n",
         "mode must be one of"),
        ("  - {id: d, type: digest, item: x}\n", "digest requires a key"),
        ("  - {id: d, type: digest, key: k}\n",
         "needs an item or a non-empty items list"),
        ("  - {id: d, type: digest, key: k, item: 7}\n",
         "item must be a template string"),
        ("  - {id: d, type: digest, key: k, items: {a: b}}\n",
         "items must be a non-empty list of template strings"),
        ("  - {id: d, type: digest, key: k, mode: flush, item: x}\n",
         "item/items only apply to accumulate"),
        ("  - {id: d, type: digest, key: k, item: x, shared: sometimes}\n",
         "shared must be true or false"),
    ])
    def test_save_rejects_bad_digest_steps(self, yaml_text, fragment):
        from src.dapier.api import designer_store

        with pytest.raises(designer_store.WorkflowError, match=fragment):
            self.parse(yaml_text)


if __name__ == "__main__":
    pytest.main([__file__])
