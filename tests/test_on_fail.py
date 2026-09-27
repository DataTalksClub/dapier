"""Per-step ``on_fail`` failure policy: halt (default) vs continue.

``on_fail: continue`` absorbs one step's failure: the step lands in run
history and the ``steps`` context as ``skipped`` (the error message rides
along), downstream steps run, and the run completes. Absent (or ``halt``) is
the historical behavior: the exception aborts the run. The registry accepts
the key on every action and rejects an unknown value at save time; the run
summary surfaces an absorbed skip additively (``had_skipped``) without
touching the run status vocabulary.
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


def run_chain(steps, *, data=None, run_action=None, hooks=None, outputs=None):
    hooks = hooks or Hooks()
    event = {**EVENT, "data": EVENT["data"] if data is None else data}
    stop = logic.run_chain(
        "wf-1", steps, event,
        run_action or (lambda action, event, workflow_id, steps=None: {"ok": True}),
        before_action=hooks.before, after_action=hooks.after,
        on_action_error=hooks.error, step_outputs=outputs,
    )
    return stop, hooks


def failing_runner(fail_for):
    """A runner that raises for ids in ``fail_for``, records the rest."""
    calls = []

    def runner(action, event, workflow_id, steps=None):
        if action.get("id") in fail_for:
            calls.append((action.get("id"), "raised"))
            raise RuntimeError("provider 500")
        calls.append((action.get("id"), "ran"))
        return {"ok": True}

    return runner, calls


class HaltTests(unittest.TestCase):
    """Absent key, empty value and explicit halt all keep today's behavior."""

    def test_default_halt_fails_the_run(self):
        runner, calls = failing_runner({"boom"})
        with pytest.raises(RuntimeError, match="provider 500"):
            run_chain(
                [
                    {"id": "boom", "type": "webhook", "url": "https://example.test"},
                    {"id": "post", "type": "webhook", "url": "https://example.test"},
                ],
                run_action=runner,
            )
        assert [call[0] for call in calls] == ["boom"]

    def test_explicit_halt_matches_the_default(self):
        runner, calls = failing_runner({"boom"})
        with pytest.raises(RuntimeError):
            run_chain(
                [
                    {"id": "boom", "type": "webhook", "url": "https://example.test",
                     "on_fail": "halt"},
                    {"id": "post", "type": "webhook", "url": "https://example.test"},
                ],
                run_action=runner,
            )
        assert [call[0] for call in calls] == ["boom"]

    def test_halt_fires_the_error_hook_and_tags_the_workflow(self):
        hooks = Hooks()
        runner, _calls = failing_runner({"boom"})
        with pytest.raises(RuntimeError) as raised:
            run_chain([{"id": "boom", "type": "webhook", "url": "https://example.test"}],
                      run_action=runner, hooks=hooks)
        assert raised.value.dapier_workflow == "wf-1"
        assert hooks.calls == [
            ("before", "boom", "webhook"),
            ("error", "boom", "provider 500"),
        ]


class ContinueTests(unittest.TestCase):
    def test_continue_records_skipped_and_runs_downstream(self):
        hooks = Hooks()
        outputs = {}
        runner, calls = failing_runner({"boom"})
        stop, _hooks = run_chain(
            [
                {"id": "boom", "type": "webhook", "url": "https://example.test",
                 "on_fail": "continue"},
                {"id": "post", "type": "webhook", "url": "https://example.test"},
            ],
            run_action=runner, hooks=hooks, outputs=outputs,
        )

        assert stop is None
        assert calls == [("boom", "raised"), ("post", "ran")]
        # The steps context: skipped, error message alongside, empty output —
        # downstream templates read {steps.boom.error} / {steps.boom.status}.
        assert outputs["boom"] == {"status": "skipped", "error": "provider 500",
                                   "output": {}}
        # Telemetry: the attempt failed (error hook), the step closed out
        # skipped through the after hook — the run itself keeps going.
        assert hooks.calls == [
            ("before", "boom", "webhook"),
            ("error", "boom", "provider 500"),
            ("after", "boom", "skipped", {}),
            ("before", "post", "webhook"),
            ("after", "post", "completed", {"ok": True}),
        ]

    def test_explicit_continue_value_only(self):
        runner, calls = failing_runner({"boom"})
        stop, _hooks = run_chain(
            [
                {"id": "boom", "type": "webhook", "url": "https://example.test",
                 "on_fail": " continue "},
                {"id": "post", "type": "webhook", "url": "https://example.test"},
            ],
            run_action=runner,
        )
        assert stop is None
        assert calls[-1] == ("post", "ran")

    def test_invalid_on_fail_value_fails_the_step_like_a_config_error(self):
        hooks = Hooks()
        runner, calls = failing_runner({"boom"})
        with pytest.raises(ValueError, match="on_fail must be one of continue, halt"):
            run_chain(
                [{"id": "boom", "type": "webhook", "url": "https://example.test",
                  "on_fail": "retry"}],
                run_action=runner, hooks=hooks,
            )
        assert calls == [("boom", "raised")]
        # The bad value is caught before any error telemetry: config
        # rejection, not a step failure (mirrors on_error's runtime check).
        assert ("error", "boom", "provider 500") not in hooks.calls

    def test_on_fail_and_on_error_are_mutually_exclusive(self):
        hooks = Hooks()
        runner, _calls = failing_runner({"boom"})
        with pytest.raises(ValueError, match="set on_fail or on_error, not both"):
            run_chain(
                [{"id": "boom", "type": "webhook", "url": "https://example.test",
                  "on_fail": "continue", "on_error": "continue"}],
                run_action=runner, hooks=hooks,
            )


class LogicStepTests(unittest.TestCase):
    """on_fail behaves the same on logic steps: the error is absorbed and the
    chain moves on (the step reads as not taken)."""

    def test_filter_config_error_continues(self):
        runner, calls = failing_runner(set())
        stop, hooks = run_chain(
            [
                {"id": "gate", "type": "filter", "field": "route",
                 "operator": "regex", "value": ".*", "on_fail": "continue"},
                {"id": "post", "type": "webhook", "url": "https://example.test"},
            ],
            run_action=runner,
        )

        assert stop is None
        assert [call[0] for call in calls] == ["post"]
        assert ("error", "gate", "unknown filter operator in rule for 'route'") in hooks.calls
        assert ("after", "gate", "skipped", {}) in hooks.calls

    def test_condition_config_error_continues(self):
        runner, calls = failing_runner(set())
        stop, _hooks = run_chain(
            [
                {"id": "route", "type": "condition", "on_fail": "continue"},
                {"id": "post", "type": "webhook", "url": "https://example.test"},
            ],
            run_action=runner,
        )

        assert stop is None
        assert [call[0] for call in calls] == ["post"]

    def test_continue_inside_a_branch_nests_the_skipped_step(self):
        hooks = Hooks()
        runner, calls = failing_runner({"boom"})
        stop, _hooks = run_chain(
            [{"id": "route", "type": "condition", "field": "route",
              "operator": "equals", "value": "invoice",
              "then": [
                  {"id": "boom", "type": "webhook", "url": "https://example.test",
                   "on_fail": "continue"},
              ]}],
            run_action=runner, hooks=hooks,
        )

        assert stop is None
        assert calls == [("boom", "raised")]
        assert ("after", "route.then.boom", "skipped", {}) in hooks.calls


class ForEachTests(unittest.TestCase):
    def test_continue_inside_the_body_lets_the_loop_go_on(self):
        hooks = Hooks()
        runner, calls = failing_runner({"upload"})
        stop, _hooks = run_chain(
            [{"id": "each", "type": "for_each", "list": "attachments",
              "actions": [
                  {"id": "upload", "type": "dropbox_upload", "connection_id": "dropbox",
                   "folder": "/Invoices/{item.filename}", "on_fail": "continue"},
              ]}],
            run_action=runner, hooks=hooks,
        )

        assert stop is None
        # Both iterations ran; the failing step was absorbed twice.
        assert [call[1] for call in calls] == ["raised", "raised"]
        assert ("after", "each", "completed",
                {"iterated": 2, "items": 2, "skipped": 0, "truncated": False}) in hooks.calls

    def test_continue_only_covers_the_marked_step(self):
        runner, calls = failing_runner({"other"})
        with pytest.raises(RuntimeError):
            run_chain(
                [{"id": "each", "type": "for_each", "list": "attachments",
                  "actions": [
                      {"id": "other", "type": "webhook", "url": "https://example.test"},
                  ]}],
                run_action=runner,
            )
        assert len(calls) == 1

    def test_loop_step_itself_continues_on_a_missing_list(self):
        runner, calls = failing_runner(set())
        stop, hooks = run_chain(
            [
                {"id": "each", "type": "for_each", "list": "nope",
                 "actions": [{"id": "upload", "type": "webhook",
                              "url": "https://example.test"}],
                 "on_fail": "continue"},
                {"id": "post", "type": "webhook", "url": "https://example.test"},
            ],
            data={}, run_action=runner,
        )

        assert stop is None
        assert [call[0] for call in calls] == ["post"]
        assert ("error", "each", "for_each 'each': list field 'nope' not found "
                "in the event data") in hooks.calls
        assert ("after", "each", "skipped", {}) in hooks.calls


class ExecuteIntegrationTests(unittest.TestCase):
    """The full engine path: a continued failure no longer kills the run."""

    def run_workflow(self, actions):
        workflow = {
            "id": "wf-on-fail", "enabled": True,
            "trigger": {"connector": "email", "event": "message.received", "filters": {}},
            "actions": actions,
        }
        hooks = Hooks()
        runner, calls = failing_runner({"boom"})
        with patch("src.dapier.engine.all_workflows", return_value=[workflow]), \
             patch("src.dapier.connectors.registry.run_action", side_effect=runner):
            matched = execute({**EVENT}, before_action=hooks.before,
                              after_action=hooks.after, on_action_error=hooks.error)
        return matched, hooks, calls

    def test_continued_failure_completes_the_run(self):
        matched, hooks, calls = self.run_workflow([
            {"id": "boom", "type": "webhook", "url": "https://example.test",
             "on_fail": "continue"},
            {"id": "post", "type": "webhook", "url": "https://example.test"},
        ])

        assert matched == ["wf-on-fail"]
        assert [call[1] for call in calls] == ["raised", "ran"]
        assert ("after", "boom", "skipped", {}) in hooks.calls
        assert ("after", "post", "completed", {"ok": True}) in hooks.calls

    def test_default_failure_still_raises(self):
        with pytest.raises(RuntimeError, match="provider 500"):
            self.run_workflow([
                {"id": "boom", "type": "webhook", "url": "https://example.test"},
            ])


class ValidationTests(unittest.TestCase):
    """The save-time contract: the key is accepted on every action, its value
    is checked, and the door stays shut for everything else."""

    def validate(self, action):
        from src.dapier.connectors.registry import validate_action_chain

        return validate_action_chain([{"type": "webhook", "url": "https://example.test",
                                       **action}])

    def test_continue_and_halt_are_accepted_on_any_action(self):
        for mode in ("continue", "halt"):
            chain = self.validate({"id": "post", "on_fail": mode})
            assert chain[0]["on_fail"] == mode

    def test_invalid_value_is_a_validation_error(self):
        from src.dapier.connectors.registry import ActionError

        with pytest.raises(ActionError, match="on_fail must be one of continue, halt"):
            self.validate({"on_fail": "retry"})

    def test_on_fail_and_on_error_conflict_fails_the_save(self):
        from src.dapier.connectors.registry import ActionError

        with pytest.raises(ActionError, match="set on_fail or on_error, not both"):
            self.validate({"on_fail": "continue", "on_error": "continue"})

    def test_other_unknown_keys_are_still_rejected(self):
        from src.dapier.connectors.registry import ActionError

        with pytest.raises(ActionError, match="unknown keys: nope"):
            self.validate({"on_fail": "continue", "nope": 1})


class RunSummaryTests(unittest.TestCase):
    """The run rolls up completed with the skip surfaced additively."""

    def _item(self, status, action_id="post"):
        return {"execution_id": f"wf-1:{action_id}:evt", "run_id": "wf-1:evt",
                "workflow_id": "wf-1", "action_id": action_id,
                "status": status, "started_at": "2026-09-25T10:00:00+00:00",
                "finished_at": "2026-09-25T10:00:01+00:00"}

    def test_skipped_step_keeps_the_run_completed_and_flags_it(self):
        from src.dapier.api import runs

        summary = runs.run_summary("wf-1:evt", [self._item("skipped", "boom"),
                                                self._item("completed")])
        assert summary["status"] == "completed"
        assert summary["had_skipped"] is True

    def test_no_skips_reads_false(self):
        from src.dapier.api import runs

        summary = runs.run_summary("wf-1:evt", [self._item("completed")])
        assert summary["had_skipped"] is False

    def test_failed_still_wins_the_rollup(self):
        from src.dapier.api import runs

        summary = runs.run_summary("wf-1:evt", [self._item("skipped", "boom"),
                                                self._item("failed", "kaput")])
        assert summary["status"] == "failed"
        assert summary["had_skipped"] is True


if __name__ == "__main__":
    pytest.main([__file__])
