"""In-workflow logic steps: paths (Zapier-Paths style n-way branching).

The first branch whose predicate matches runs its actions as a nested chain;
with no match the optional ``default`` steps run. A filter inside a branch
stops the chain quietly, exactly like condition branches.
"""
import unittest

import pytest

from src.dapier.engine import logic


EVENT = {
    "id": "evt-1",
    "connector": "email",
    "event": "message.received",
    "data": {"route": "invoice", "subject": "Invoice 42 from Acme"},
}


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


def run_chain(steps, *, data=None, run_action=None, hooks=None):
    hooks = hooks or Hooks()
    event = {**EVENT, "data": EVENT["data"] if data is None else data}
    stop = logic.run_chain(
        "wf-1", steps, event,
        run_action or (lambda action, event, workflow_id, steps=None: {"ok": True}),
        before_action=hooks.before, after_action=hooks.after,
        on_action_error=hooks.error,
    )
    return stop, hooks


def record(ran):
    return lambda action, event, workflow_id, steps=None: ran.append(action["id"]) or {}


ROUTE_STEP = {
    "id": "route",
    "type": "paths",
    "paths": [
        {"label": "invoices", "when": {"subject": {"contains": "Invoice"}},
         "actions": [{"id": "post", "type": "slack", "channel": "#inv"}]},
        {"label": "receipts", "field": "subject", "operator": "prefix",
         "value": "Receipt", "actions": [{"id": "file", "type": "webhook",
                                          "url": "https://example.test"}]},
    ],
    "default": [{"id": "fallback", "type": "webhook", "url": "https://example.test"}],
}


class PathsTests(unittest.TestCase):
    def test_first_matching_branch_wins(self):
        ran = []
        stop, hooks = run_chain([ROUTE_STEP], run_action=record(ran))

        assert stop is None
        assert ran == ["post"]
        assert ("before", "route.invoices.post", "slack") in hooks.calls
        assert ("after", "route", "completed",
                {"matched": "invoices", "ran": 1}) in hooks.calls

    def test_later_predicates_are_not_evaluated_after_a_match(self):
        ran = []
        stop, _hooks = run_chain(
            [{"id": "route", "type": "paths", "paths": [
                {"label": "first", "when": {"route": {"equals": "invoice"}},
                 "actions": [{"id": "post", "type": "webhook", "url": "https://x"}]},
                # An unknown operator raises ValueError when evaluated; a match
                # earlier in the list must never reach it.
                {"label": "boom", "field": "subject", "operator": "regex",
                 "value": ".*", "actions": []},
            ]}],
            run_action=record(ran),
        )

        assert stop is None
        assert ran == ["post"]

    def test_flat_shorthand_branch_matches(self):
        ran = []
        stop, hooks = run_chain([ROUTE_STEP], data={"subject": "Receipt 7 from Acme"},
                                run_action=record(ran))

        assert stop is None
        assert ran == ["file"]
        assert ("after", "route", "completed",
                {"matched": "receipts", "ran": 1}) in hooks.calls

    def test_no_match_runs_the_default_branch_and_the_chain_continues(self):
        ran = []
        stop, hooks = run_chain(
            [ROUTE_STEP, {"id": "post", "type": "webhook", "url": "https://x"}],
            data={"subject": "Hello world"}, run_action=record(ran),
        )

        assert stop is None
        assert ran == ["fallback", "post"]
        assert ("before", "route.default.fallback", "webhook") in hooks.calls
        assert ("after", "route", "completed",
                {"matched": None, "ran": 1}) in hooks.calls

    def test_no_match_without_default_continues_the_chain(self):
        ran = []
        step = {key: value for key, value in ROUTE_STEP.items() if key != "default"}
        stop, hooks = run_chain(
            [step, {"id": "post", "type": "webhook", "url": "https://x"}],
            data={"subject": "Hello world"}, run_action=record(ran),
        )

        assert stop is None
        assert ran == ["post"]
        assert ("after", "route", "completed",
                {"matched": None, "ran": 0}) in hooks.calls

    def test_filtered_stop_inside_a_branch_propagates(self):
        ran = []
        step = {key: value for key, value in ROUTE_STEP.items() if key != "default"}
        step["paths"] = [{
            "label": "gated",
            "when": {"subject": {"contains": "Invoice"}},
            "actions": [
                {"id": "gate", "type": "filter", "field": "route",
                 "operator": "equals", "value": "receipt"},
                {"id": "post", "type": "webhook", "url": "https://x"},
            ],
        }]
        stop, hooks = run_chain(
            [step, {"id": "sibling", "type": "webhook", "url": "https://x"}],
            run_action=record(ran),
        )

        assert stop == "filtered"
        assert ran == []
        assert ("after", "route.gated.gate", "filtered",
                {"filter": "stopped", "when": {"route": {"equals": "receipt"}}}) in hooks.calls
        assert not any(call[0] == "error" for call in hooks.calls)

    def test_bad_shapes_are_config_errors(self):
        with pytest.raises(ValueError, match="non-empty list of paths"):
            run_chain([{"id": "route", "type": "paths", "paths": []}])
        with pytest.raises(ValueError, match="needs a when mapping or a field"):
            run_chain([{"id": "route", "type": "paths",
                        "paths": [{"label": "a", "actions": []}]}])
        with pytest.raises(ValueError, match="default must be a list of steps"):
            run_chain([{"id": "route", "type": "paths", "paths": ROUTE_STEP["paths"],
                        "default": {"id": "b"}}])

    def test_paths_inside_for_each_see_the_item_scope(self):
        ran = []
        stop, hooks = run_chain(
            [{"id": "each", "type": "for_each", "list": "attachments",
              "actions": [{"id": "pick", "type": "paths", "paths": [
                  {"label": "pdf", "field": "item.filename", "operator": "suffix",
                   "value": ".pdf", "actions": [{"id": "upload", "type": "webhook"}]},
              ], "default": [{"id": "park", "type": "webhook", "url": "https://x"}]}]}],
            data={"attachments": [{"filename": "a.pdf"}, {"filename": "b.txt"}]},
            run_action=record(ran),
        )

        assert stop is None
        assert ran == ["upload", "park"]
        assert ("before", "each[0].pick.pdf.upload", "webhook") in hooks.calls
        assert ("before", "each[1].pick.default.park", "webhook") in hooks.calls
