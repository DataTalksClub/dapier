"""Filter steps that see earlier step outputs, and the full operator set
everywhere rules are written.

The rule evaluator (engine.matching._matches_filter) grew the comparison and
presence operators; predicates in the chain (filter, condition, paths and the
for_each list) now read the accumulated ``steps`` outputs next to the trigger
data, so mid-chain routing can react to what earlier steps found. The same
operators validate at save time (designer_store.FILTER_OPERATORS) and are
legal in trigger filters (matching.matches).
"""
import unittest

import pytest
import yaml

from src.dapier.api import designer_store
from src.dapier.engine.matching import matches

from tests.test_logic import run_chain


EVENT = {
    "id": "evt-1",
    "connector": "email",
    "event": "message.received",
    "data": {"route": "invoice", "subject": "Invoice 42", "count": "3"},
}


# --- the chain: rules read steps outputs ------------------------------------

def test_filter_passes_on_a_steps_output():
    steps = [
        {"id": "find", "type": "webhook", "url": "https://example.test"},
        {"id": "gate", "type": "filter", "field": "steps.find.output.count",
         "operator": "gt", "value": 0},
        {"id": "send", "type": "webhook", "url": "https://example.test"},
    ]

    calls = []

    def run_action(action, event, workflow_id, steps=None):
        calls.append(action["id"])
        if action["id"] == "find":
            return {"count": 2}
        return {"ok": True}

    stop, _hooks, outputs = run_chain(steps, data=EVENT["data"], run_action=run_action)
    assert stop is None
    assert calls == ["find", "send"]
    assert outputs["gate"]["status"] == "completed"


def test_filter_stops_when_the_steps_output_fails_the_rule():
    steps = [
        {"id": "gate", "type": "filter",
         "when": {"steps.find.output.count": {"gt": 0}}},
        {"id": "send", "type": "webhook", "url": "https://example.test"},
    ]

    stop, _hooks, outputs = run_chain(
        steps, data=EVENT["data"],
        step_outputs={"find": {"status": "completed", "output": {"count": 0}}})
    assert stop == "filtered"
    assert outputs["gate"]["output"]["filter"] == "stopped"


def test_condition_branches_on_a_steps_output():
    steps = [{
        "id": "route",
        "type": "condition",
        "when": {"steps.lookup.output.found": {"equals": True}},
        "then": [{"id": "yes", "type": "webhook", "url": "https://example.test"}],
        "else": [{"id": "no", "type": "webhook", "url": "https://example.test"}],
    }]

    _stop, _hooks, outputs = run_chain(
        steps, data=EVENT["data"],
        step_outputs={"lookup": {"status": "completed", "output": {"found": False}}})
    assert "route.else.no" in outputs
    assert "route.then.yes" not in outputs


def test_paths_picks_a_branch_on_a_steps_output():
    steps = [{
        "id": "route",
        "type": "paths",
        "paths": [
            {"label": "rich", "when": {"steps.score.output.total": {"gte": 10}},
             "actions": [{"id": "a", "type": "webhook", "url": "https://example.test"}]},
            {"label": "poor", "field": "steps.score.output.total",
             "operator": "lt", "value": 10,
             "actions": [{"id": "b", "type": "webhook", "url": "https://example.test"}]},
        ],
    }]

    _stop, _hooks, outputs = run_chain(
        steps, data=EVENT["data"],
        step_outputs={"score": {"status": "completed", "output": {"total": 12}}})
    assert outputs["route"]["output"]["matched"] == "rich"


def test_for_each_iterates_a_steps_output_list():
    steps = [{
        "id": "each",
        "type": "for_each",
        "list": "steps.find.output.rows",
        "actions": [{"id": "post", "type": "webhook",
                     "url": "https://example.test/{item}"}],
    }]

    calls = []

    def run_action(action, event, workflow_id, steps=None):
        calls.append(action["url"])
        return {"ok": True}

    _stop, _hooks, outputs = run_chain(
        steps, data=EVENT["data"],
        step_outputs={"find": {"status": "completed",
                               "output": {"rows": ["r1", "r2"]}}},
        run_action=run_action)
    assert calls == ["https://example.test/r1", "https://example.test/r2"]
    assert outputs["each"]["output"]["iterated"] == 2


def test_loop_scope_wins_over_the_trigger_data_for_item_rules():
    steps = [{
        "id": "each",
        "type": "for_each",
        "list": "rows",
        "actions": [{"id": "gate", "type": "filter",
                     "when": {"item": {"equals": "keep"}}}],
    }]

    _stop, _hooks, outputs = run_chain(steps, data={"rows": ["keep", "drop"]})
    # A filter inside the body skips just its iteration; the loop goes on.
    assert outputs["each"]["status"] == "completed"
    assert outputs["each"]["output"]["iterated"] == 2
    assert outputs["each"]["output"]["skipped"] == 1


def test_trigger_data_still_reads_without_steps():
    steps = [{"id": "gate", "type": "filter", "field": "route",
              "operator": "equals", "value": "invoice"}]
    stop, _hooks, outputs = run_chain(steps, data=EVENT["data"])
    assert stop is None
    assert outputs["gate"]["output"]["filter"] == "passed"


# --- the operator set, everywhere -------------------------------------------

class OperatorTests(unittest.TestCase):
    def rule(self, operator, expected, value):
        """A filter on trigger data ``value`` with the rule {operator: expected}."""
        _stop, _hooks, outputs = run_chain(
            [{"id": "gate", "type": "filter",
              "when": {"field": {operator: expected}}}],
            data={"field": value})
        return outputs["gate"]["output"]["filter"] == "passed"

    def test_numeric_ordering(self):
        assert self.rule("gt", 9, "9") is False       # 9 > 9
        assert self.rule("gt", 9, 10) is True         # numeric, not lexicographic
        assert self.rule("gte", 10, 10) is True
        assert self.rule("lt", 10, "9") is True       # 9 < 10
        assert self.rule("lt", 10, 10) is False
        assert self.rule("lte", 10, "9") is True      # 9 <= 10
        assert self.rule("lte", 9, 10) is False       # 10 <= 9

    def test_ordering_falls_back_to_strings(self):
        assert self.rule("gt", "2026-01-01", "2026-09-28") is True
        assert self.rule("gt", "a", "b") is True
        assert self.rule("gte", "b", "a") is False

    def test_negations(self):
        assert self.rule("not_equals", "a", "b") is True
        assert self.rule("not_equals", "a", "a") is False
        assert self.rule("does_not_contain", "x", "abc") is True
        assert self.rule("does_not_contain", "b", "abc") is False

    def test_presence(self):
        assert self.rule("exists", True, "any") is True
        assert self.rule("exists", True, "") is False
        assert self.rule("exists", True, None) is False   # missing path reads empty
        assert self.rule("empty", True, "") is True
        assert self.rule("empty", False, "any") is True


def test_trigger_filters_accept_the_new_operators():
    workflow = {
        "id": "wf",
        "enabled": True,
        "triggers": [{"connector": "email", "event": "message.received",
                      "filters": {"count": {"gt": 5}, "subject": {"exists": True}}}],
    }
    assert matches(workflow, {**EVENT, "data": {"count": 7, "subject": "hi"}}) is True
    assert matches(workflow, {**EVENT, "data": {"count": 3, "subject": "hi"}}) is False
    assert matches(workflow, {**EVENT, "data": {"count": 7}}) is False


def test_unknown_operator_is_a_loud_config_error():
    with pytest.raises(ValueError, match="unknown filter operator"):
        run_chain([{"id": "gate", "type": "filter",
                    "when": {"route": {"nope": 1}}}], data=EVENT["data"])


# --- save time: the designer accepts what the engine accepts ----------------

def _designer_yaml(actions):
    return yaml.safe_dump({
        "id": "test-flow",
        "trigger": {"connector": "email", "event": "message.received"},
        "actions": actions,
    }, sort_keys=True)


@pytest.mark.parametrize("operator", [
    "equals", "not_equals", "in", "prefix", "suffix", "contains",
    "does_not_contain", "gt", "gte", "lt", "lte", "exists", "empty",
])
def test_designer_saves_every_engine_operator(operator):
    designer_store.parse_workflow(_designer_yaml([
        {"id": "gate", "type": "filter", "field": "count",
         "operator": operator, "value": "1"},
        {"id": "post", "type": "webhook", "url": "https://example.test"},
    ]))


def test_designer_still_rejects_unknown_operators():
    with pytest.raises(designer_store.WorkflowError, match="operator must be one of"):
        designer_store.parse_workflow(_designer_yaml([
            {"id": "gate", "type": "filter", "field": "count",
             "operator": "nope", "value": "1"},
        ]))


def test_designer_saves_autoretry_on_connector_actions():
    designer_store.parse_workflow(_designer_yaml([
        {"id": "post", "type": "webhook", "url": "https://example.test",
         "autoretry": {"attempts": 2}},
    ]))


def test_designer_rejects_bad_autoretry_shapes():
    with pytest.raises(designer_store.WorkflowError, match="autoretry attempts"):
        designer_store.parse_workflow(_designer_yaml([
            {"id": "post", "type": "webhook", "url": "https://example.test",
             "autoretry": {"attempts": 99}},
        ]))
    with pytest.raises(designer_store.WorkflowError,
                       match="autoretry only applies to connector actions"):
        designer_store.parse_workflow(_designer_yaml([
            {"id": "gate", "type": "filter", "field": "route",
             "operator": "equals", "value": "x", "autoretry": {"attempts": 1}},
        ]))


if __name__ == "__main__":
    pytest.main([__file__])
