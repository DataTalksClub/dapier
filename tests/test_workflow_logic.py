"""In-workflow logic steps: filter, condition, delay, for_each (issue #7).

The chain runner gives every step the same telemetry the connector actions
get; a failed filter stops the chain quietly with a ``filtered`` status
instead of an error.
"""
import unittest
from unittest.mock import patch

import pytest

from src.dapier.engine import execute, logic


EVENT = {
    "id": "evt-1",
    "connector": "email",
    "event": "message.received",
    "data": {
        "route": "invoice",
        "subject": "Invoice 42 from Acme",
        "attachments": [
            {"filename": "a.pdf", "size": 1},
            {"filename": "b.pdf", "size": 2},
        ],
    },
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


class FilterTests(unittest.TestCase):
    def test_pass_runs_the_rest_of_the_chain(self):
        runner_calls = []
        stop, hooks = run_chain(
            [
                {"id": "gate", "type": "filter", "field": "route", "operator": "equals", "value": "invoice"},
                {"id": "post", "type": "webhook", "url": "https://example.test"},
            ],
            run_action=lambda action, event, workflow_id, steps=None: runner_calls.append(action["id"]) or {},
        )

        assert stop is None
        assert runner_calls == ["post"]
        assert ("after", "gate", "completed", {"filter": "passed"}) in hooks.calls
        assert ("after", "post", "completed", {}) in hooks.calls

    def test_fail_stops_quietly_with_filtered_status(self):
        runner_calls = []
        stop, hooks = run_chain(
            [
                {"id": "gate", "type": "filter", "field": "subject", "operator": "prefix", "value": "Receipt"},
                {"id": "post", "type": "webhook", "url": "https://example.test"},
            ],
            run_action=lambda action, event, workflow_id, steps=None: runner_calls.append(action["id"]),
        )

        assert stop == "filtered"
        assert runner_calls == []
        assert ("after", "gate", "filtered",
                {"filter": "stopped", "when": {"subject": {"prefix": "Receipt"}}}) in hooks.calls
        assert not any(call[0] == "error" for call in hooks.calls)

    def test_when_mapping_requires_every_rule(self):
        stop, hooks = run_chain(
            [{"id": "gate", "type": "filter",
              "when": {"route": {"equals": "invoice"}, "subject": {"contains": "Acme"}}}],
        )
        assert stop is None
        assert ("after", "gate", "completed", {"filter": "passed"}) in hooks.calls

        stop, hooks = run_chain(
            [{"id": "gate", "type": "filter",
              "when": {"route": {"equals": "invoice"}, "subject": {"contains": "Urgent"}}}],
        )
        assert stop == "filtered"

    def test_missing_field_reads_as_empty(self):
        stop, _hooks = run_chain(
            [{"id": "gate", "type": "filter", "field": "nope", "operator": "equals", "value": ""}],
            data={"route": "invoice"},
        )
        assert stop is None  # "" == "" passes, like trigger filters

    def test_unknown_operator_is_a_step_error_not_a_filter_stop(self):
        hooks = Hooks()
        with pytest.raises(ValueError, match="unknown filter operator"):
            run_chain(
                [{"id": "gate", "type": "filter", "field": "route",
                  "operator": "regex", "value": ".*"}],
                hooks=hooks,
            )
        assert hooks.calls == [
            ("before", "gate", "filter"),
            ("error", "gate", "unknown filter operator in rule for 'route'"),
        ]

    def test_without_a_predicate_is_a_config_error(self):
        with pytest.raises(ValueError, match="needs a when mapping or a field"):
            run_chain([{"id": "gate", "type": "filter"}])


class ConditionTests(unittest.TestCase):
    def test_true_predicate_runs_the_then_branch(self):
        runner_calls = []
        stop, hooks = run_chain(
            [
                {"id": "route", "type": "condition", "field": "route", "operator": "equals",
                 "value": "invoice",
                 "then": [{"id": "notify", "type": "slack", "channel": "#inv"}],
                 "else": [{"id": "log", "type": "webhook", "url": "https://example.test"}]},
            ],
            run_action=lambda action, event, workflow_id, steps=None: runner_calls.append(action["id"]) or {},
        )

        assert stop is None
        assert runner_calls == ["notify"]
        assert ("after", "route", "completed",
                {"condition": "passed", "branch": "then", "steps": 1}) in hooks.calls

    def test_false_predicate_runs_the_else_branch(self):
        runner_calls = []
        stop, _hooks = run_chain(
            [
                {"id": "route", "type": "condition", "when": {"route": {"equals": "receipt"}},
                 "then": [{"id": "notify", "type": "slack", "channel": "#inv"}],
                 "else": [{"id": "log", "type": "webhook", "url": "https://example.test"}]},
            ],
            run_action=lambda action, event, workflow_id, steps=None: runner_calls.append(action["id"]) or {},
        )

        assert stop is None
        assert runner_calls == ["log"]

    def test_sub_steps_nest_their_ids_under_the_branch(self):
        _stop, hooks = run_chain(
            [{"id": "route", "type": "condition", "field": "route", "operator": "equals",
              "value": "invoice",
              "then": [{"id": "notify", "type": "slack", "channel": "#inv"}]}],
        )
        assert ("before", "route.then.notify", "slack") in hooks.calls
        assert ("after", "route.then.notify", "completed", {"ok": True}) in hooks.calls

    def test_empty_branches_act_as_a_gate(self):
        stop, hooks = run_chain(
            [{"id": "gate", "type": "condition", "field": "route", "operator": "equals",
              "value": "todo"}],
        )
        assert stop is None
        assert ("after", "gate", "completed", {"condition": "failed", "branch": "else",
                                               "steps": 0}) in hooks.calls

    def test_non_list_branch_is_a_config_error(self):
        with pytest.raises(ValueError, match="must be a list of steps"):
            run_chain(
                [{"id": "route", "type": "condition", "when": {"route": {"equals": "invoice"}},
                  "then": {"id": "notify"}}],
            )


class DelayTests(unittest.TestCase):
    def test_sleeps_and_reports(self):
        with patch("src.dapier.engine.logic.time") as fake_time:
            fake_time.monotonic.side_effect = [0.0, 0.05]
            fake_time.sleep.return_value = None
            stop, hooks = run_chain([{"id": "pause", "type": "delay", "seconds": 30}])

        assert stop is None
        fake_time.sleep.assert_called_once_with(30)
        assert ("after", "pause", "completed",
                {"delay_seconds": 30, "slept_seconds": 30}) in hooks.calls

    def test_clamps_to_the_lambda_limit(self):
        with patch("src.dapier.engine.logic.time") as fake_time:
            fake_time.monotonic.side_effect = [0.0, 0.05]
            stop, hooks = run_chain([{"id": "pause", "type": "delay", "seconds": 3600}])

        assert stop is None
        fake_time.sleep.assert_called_once_with(logic.MAX_DELAY_SECONDS)
        assert ("after", "pause", "completed",
                {"delay_seconds": 3600, "slept_seconds": logic.MAX_DELAY_SECONDS}) in hooks.calls

    def test_bad_seconds_is_a_config_error(self):
        for seconds in (None, "30", True, -1):
            with self.subTest(seconds=seconds), \
                    pytest.raises(ValueError, match="needs seconds"):
                run_chain([{"id": "pause", "type": "delay", "seconds": seconds}])


class ForEachTests(unittest.TestCase):
    BODY = [{"id": "upload", "type": "dropbox_upload", "connection_id": "dropbox",
             "folder": "/Invoices/{item.filename}"}]

    def run_loop(self, body=None, step=None, data=None):
        runner_calls = []
        step = step or {"id": "each", "type": "for_each", "list": "attachments",
                        "actions": body if body is not None else self.BODY}
        stop, hooks = run_chain(
            [step],
            data={"attachments": EVENT["data"]["attachments"]} if data is None else data,
            run_action=lambda action, event, workflow_id, steps=None:
                runner_calls.append(dict(action)) or {"ok": True},
        )
        return stop, hooks, runner_calls

    def test_runs_the_body_once_per_item_with_item_bound(self):
        stop, hooks, runner_calls = self.run_loop()

        assert stop is None
        assert [action["folder"] for action in runner_calls] == \
            ["/Invoices/a.pdf", "/Invoices/b.pdf"]
        assert ("after", "each", "completed",
                {"iterated": 2, "items": 2, "skipped": 0, "truncated": False}) in hooks.calls
        # The runner still gets the raw event; templating happened on the step.
        assert ("before", "each[0].upload", "dropbox_upload") in hooks.calls
        assert ("before", "each[1].upload", "dropbox_upload") in hooks.calls

    def test_scalar_items_and_custom_item_variable(self):
        _stop, _hooks, runner_calls = self.run_loop(
            step={"id": "each", "type": "for_each", "list": "hosts", "item": "name",
                  "actions": [{"id": "ping", "type": "webhook",
                               "url": "https://example.test/{name}/{name_index}"}]},
            data={"hosts": ["a.test", "b.test"]},
        )

        assert [action["url"] for action in runner_calls] == \
            ["https://example.test/a.test/0", "https://example.test/b.test/1"]

    def test_unknown_tokens_survive_for_the_connector_runner(self):
        _stop, _hooks, runner_calls = self.run_loop(
            body=[{"id": "upload", "type": "dropbox_upload",
                   "folder": "/Invoices/{item.filename}/{subject}"}],
        )

        # {item.filename} resolved; {subject} is left for the runner's own
        # templating against the event data.
        assert runner_calls[0]["folder"] == "/Invoices/a.pdf/{subject}"

    def test_max_iterations_caps_the_loop(self):
        stop, hooks, runner_calls = self.run_loop(
            step={"id": "each", "type": "for_each", "list": "attachments",
                  "max_iterations": 1, "actions": self.BODY},
        )

        assert stop is None
        assert len(runner_calls) == 1
        assert ("after", "each", "completed",
                {"iterated": 1, "items": 2, "skipped": 0, "truncated": True}) in hooks.calls

    def test_hard_cap_protects_the_lambda(self):
        items = [{"filename": f"{n}.pdf"} for n in range(250)]
        _stop, hooks, runner_calls = self.run_loop(
            step={"id": "each", "type": "for_each", "list": "attachments",
                  "actions": self.BODY},
            data={"attachments": items},
        )

        assert len(runner_calls) == logic.MAX_LOOP_ITERATIONS
        assert hooks.calls[-1][3]["truncated"] is True

    def test_filter_inside_the_body_skips_one_item(self):
        stop, hooks, runner_calls = self.run_loop(
            body=[
                {"id": "big-only", "type": "filter",
                 "when": {"item.size": {"prefix": "2"}}},
                {"id": "upload", "type": "dropbox_upload", "connection_id": "dropbox",
                 "folder": "/Invoices/{item.filename}"},
            ],
        )

        assert stop is None
        assert [action["folder"] for action in runner_calls] == ["/Invoices/b.pdf"]
        assert ("after", "each", "completed",
                {"iterated": 2, "items": 2, "skipped": 1, "truncated": False}) in hooks.calls

    def test_missing_list_is_a_config_error(self):
        with pytest.raises(ValueError, match="not found in the event data"):
            self.run_loop(step={"id": "each", "type": "for_each", "list": "nope",
                           "actions": self.BODY}, data={})

    def test_non_list_field_is_a_config_error(self):
        with pytest.raises(ValueError, match="is not a list"):
            self.run_loop(step={"id": "each", "type": "for_each", "list": "attachments",
                           "actions": self.BODY}, data={"attachments": "a.pdf"})

    def test_empty_body_is_a_config_error(self):
        with pytest.raises(ValueError, match="at least one step"):
            self.run_loop(step={"id": "each", "type": "for_each", "list": "attachments",
                           "actions": []})

    def test_without_a_list_field_is_a_config_error(self):
        with pytest.raises(ValueError, match="needs a list field"):
            self.run_loop(step={"id": "each", "type": "for_each", "actions": self.BODY})


class ExecuteIntegrationTests(unittest.TestCase):
    """The full engine path: matching, the chain, and the telemetry hooks."""

    def run_workflow(self, actions, data=None):
        workflow = {
            "id": "wf-logic", "enabled": True,
            "trigger": {"connector": "email", "event": "message.received", "filters": {}},
            "actions": actions,
        }
        event = {**EVENT, "data": EVENT["data"] if data is None else data}
        hooks = Hooks()
        with patch("src.dapier.engine.all_workflows", return_value=[workflow]), \
             patch("src.dapier.connectors.registry.run_action", return_value={"status": 200}) as run_webhook:
            execute(event, before_action=hooks.before, after_action=hooks.after,
                    on_action_error=hooks.error)
        return hooks, run_webhook

    def test_filter_pass_then_action_runs(self):
        hooks, run_webhook = self.run_workflow([
            {"id": "gate", "type": "filter", "field": "route", "operator": "equals",
             "value": "invoice"},
            {"id": "post", "type": "webhook", "url": "https://example.test"},
        ])

        assert run_webhook.call_count == 1
        assert ("before", "post", "webhook") in hooks.calls
        assert ("after", "post", "completed", {"status": 200}) in hooks.calls

    def test_filter_fail_marks_the_step_filtered(self):
        hooks, run_webhook = self.run_workflow([
            {"id": "gate", "type": "filter", "field": "subject", "operator": "suffix",
             "value": "!!!"},
            {"id": "post", "type": "webhook", "url": "https://example.test"},
        ])

        assert run_webhook.call_count == 0
        assert ("after", "gate", "filtered",
                {"filter": "stopped", "when": {"subject": {"suffix": "!!!"}}}) in hooks.calls
        assert not any(call[0] == "error" for call in hooks.calls)

    def test_condition_branches_drive_the_connector(self):
        steps = [{"id": "route", "type": "condition", "field": "route",
                  "operator": "equals", "value": "invoice",
                  "then": [{"id": "invoice-hook", "type": "webhook",
                            "url": "https://example.test/inv"}],
                  "else": [{"id": "other-hook", "type": "webhook",
                            "url": "https://example.test/other"}]}]

        hooks, run_webhook = self.run_workflow(steps)
        assert [call.args[0]["url"] for call in run_webhook.call_args_list] == \
            ["https://example.test/inv"]
        assert ("after", "route", "completed",
                {"condition": "passed", "branch": "then", "steps": 1}) in hooks.calls

        hooks, run_webhook = self.run_workflow(steps, data={"route": "receipt"})
        assert [call.args[0]["url"] for call in run_webhook.call_args_list] == \
            ["https://example.test/other"]

    def test_nested_condition_for_each_and_filter(self):
        hooks, run_webhook = self.run_workflow([
            {"id": "route", "type": "condition", "field": "route", "operator": "equals",
             "value": "invoice",
             "then": [
                 {"id": "each", "type": "for_each", "list": "attachments",
                  "actions": [
                      {"id": "big-only", "type": "filter",
                       "when": {"item.size": {"prefix": "2"}}},
                      {"id": "post", "type": "webhook", "url": "https://example.test/{item.filename}"},
                  ]},
             ]},
        ], data={"route": "invoice", "attachments": [
            {"filename": "small.pdf", "size": 1},
            {"filename": "big.pdf", "size": 2},
        ]})

        urls = [call.args[0]["url"] for call in run_webhook.call_args_list]
        assert urls == ["https://example.test/big.pdf"]
        assert ("before", "route.then.each[0].big-only", "filter") in hooks.calls
        assert ("before", "route.then.each[1].post", "webhook") in hooks.calls
        assert ("after", "route.then.each", "completed",
                {"iterated": 2, "items": 2, "skipped": 1, "truncated": False}) in hooks.calls

    def test_delay_runs_between_steps(self):
        workflow_actions = [
            {"id": "pause", "type": "delay", "seconds": 5},
            {"id": "post", "type": "webhook", "url": "https://example.test"},
        ]
        hooks = Hooks()
        with patch("src.dapier.engine.all_workflows",
                   return_value=[{"id": "wf-logic", "enabled": True,
                                  "trigger": {"connector": "email",
                                              "event": "message.received",
                                              "filters": {}},
                                  "actions": workflow_actions}]), \
             patch("src.dapier.connectors.registry.run_action",
                   lambda action, event, workflow_id=None, steps=None: {"status": 200}), \
             patch("src.dapier.engine.logic.time") as fake_time:
            fake_time.monotonic.side_effect = [0.0, 0.01, 0.02, 0.03]
            execute({**EVENT}, before_action=hooks.before, after_action=hooks.after,
                    on_action_error=hooks.error)

        fake_time.sleep.assert_called_once_with(5)
        assert ("after", "pause", "completed",
                {"delay_seconds": 5, "slept_seconds": 5}) in hooks.calls

    def test_processed_steps_are_skipped_like_actions(self):
        hooks = Hooks(pending=False)  # the ledger says: already ran
        with patch("src.dapier.engine.all_workflows",
                   return_value=[{"id": "wf-logic", "enabled": True,
                                  "trigger": {"connector": "email",
                                              "event": "message.received",
                                              "filters": {}},
                                  "actions": [{"id": "gate", "type": "filter",
                                               "field": "route", "operator": "equals",
                                               "value": "invoice"}]}]), \
             patch("src.dapier.engine.logic.time") as fake_time:
            execute({**EVENT}, before_action=hooks.before, after_action=hooks.after,
                    on_action_error=hooks.error)

        assert hooks.calls == [("before", "gate", "filter")]
        fake_time.sleep.assert_not_called()


if __name__ == "__main__":
    pytest.main([__file__])
