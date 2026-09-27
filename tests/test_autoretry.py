"""Per-step autoretry (Zapier's autoretry) on connector actions.

``autoretry: {attempts, initial_seconds, max_seconds}`` retries a failed
action in place with exponential backoff before any error policy applies;
once the retries are exhausted the failure falls through to ``on_fail``/
``on_error`` exactly as a single attempt would. The step's output records
``attempts`` (tries made), so run history shows a step that succeeded on
try 3. Retry is opt-in: without the key nothing changes. Save-time
validation (``registry.validate_autoretry_key``) shares the engine's checks
and rejects bad bounds and the key on logic steps.
"""
import unittest
from unittest.mock import patch

import pytest

from src.dapier.connectors import registry
from src.dapier.engine import execute, logic


EVENT = {
    "id": "evt-1",
    "connector": "email",
    "event": "message.received",
    "data": {"route": "invoice", "subject": "Invoice 42"},
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
    outputs = {} if outputs is None else outputs
    event = {**EVENT, "data": EVENT["data"] if data is None else data}
    stop = logic.run_chain(
        "wf-1", steps, event,
        run_action or (lambda action, event, workflow_id, steps=None: {"ok": True}),
        before_action=hooks.before, after_action=hooks.after,
        on_action_error=hooks.error, step_outputs=outputs,
    )
    return stop, hooks, outputs


def flaky_runner(fail_times=1, error=RuntimeError("provider 429"), step_id=None):
    """A runner that fails the first ``fail_times`` invocations of ``step_id``
    (or of each step, when no id is given), then succeeds."""
    calls = []

    def runner(action, event, workflow_id, steps=None):
        action_id = action.get("id")
        calls.append(action_id)
        if (step_id is None or action_id == step_id) \
                and calls.count(action_id) <= fail_times:
            raise error
        return {"ok": True}

    return runner, calls


class AutoretryTests(unittest.TestCase):
    """The happy path: a transient failure retries and the chain moves on."""

    def test_transient_failure_succeeds_on_the_second_try(self):
        runner, calls = flaky_runner(fail_times=1)
        steps = [{"id": "post", "type": "webhook", "url": "https://example.test",
                  "autoretry": {"attempts": 2}}]
        with patch("src.dapier.engine.logic.time.sleep") as fake_sleep:
            stop, hooks, outputs = run_chain(steps, run_action=runner)

        assert stop is None
        assert calls == ["post", "post"]  # one retry, then success
        assert ("after", "post", "completed", {"ok": True, "attempts": 2}) in hooks.calls
        fake_sleep.assert_called_once()  # one backoff sleep before the retry

    def test_succeeds_on_the_last_allowed_try(self):
        runner, calls = flaky_runner(fail_times=2)
        steps = [{"id": "post", "type": "webhook", "url": "https://example.test",
                  "autoretry": {"attempts": 2}}]
        with patch("src.dapier.engine.logic.time.sleep") as fake_sleep:
            _stop, hooks, outputs = run_chain(steps, run_action=runner)

        assert len(calls) == 3  # total tries = attempts + 1
        assert outputs["post"]["output"]["attempts"] == 3
        assert fake_sleep.call_count == 2

    def test_backoff_doubles_and_caps_with_jitter(self):
        plan = {"attempts": 3, "initial_seconds": 2, "max_seconds": 5}
        with patch("src.dapier.engine.logic.random.uniform", return_value=0.0) as jitter, \
                patch("src.dapier.engine.logic.time.sleep") as fake_sleep:
            runner, _calls = flaky_runner(fail_times=3)
            run_chain([{"id": "post", "type": "webhook", "url": "https://example.test",
                        "autoretry": plan}], run_action=runner)

        # 2s doubling, capped at max_seconds=5; jitter queried once per retry.
        assert [call.args[0] for call in fake_sleep.call_args_list] == [2.0, 4.0, 5.0]
        assert jitter.call_count == 3

    def test_defaults_fill_the_unnamed_seconds(self):
        plan = logic._autoretry_plan(
            {"type": "webhook", "autoretry": {"attempts": 1}}, "post")
        assert plan == {"attempts": 1, "initial_seconds": 1.0, "max_seconds": 60.0}

    def test_no_autoretry_key_is_a_single_attempt(self):
        runner, calls = flaky_runner(fail_times=1)
        with pytest.raises(RuntimeError, match="provider 429"):
            run_chain([{"id": "post", "type": "webhook", "url": "https://example.test"}],
                      run_action=runner)
        assert calls == ["post"]  # no retry without the opt-in

    def test_no_key_keeps_the_output_untouched(self):
        runner, _calls = flaky_runner(fail_times=0)
        _stop, _hooks, outputs = run_chain(
            [{"id": "post", "type": "webhook", "url": "https://example.test"}],
            run_action=runner)
        assert outputs["post"] == {"status": "completed", "output": {"ok": True}}


class ExhaustedTests(unittest.TestCase):
    """Once the retries run out, the existing error policies apply unchanged."""

    def test_halt_raises_after_the_last_try(self):
        hooks = Hooks()
        runner, calls = flaky_runner(fail_times=99, step_id="post")
        steps = [{"id": "post", "type": "webhook", "url": "https://example.test",
                  "autoretry": {"attempts": 2}}]
        with patch("src.dapier.engine.logic.time.sleep"):
            with pytest.raises(RuntimeError, match="provider 429"):
                run_chain(steps, run_action=runner, hooks=hooks)

        assert len(calls) == 3  # attempts + 1 tries, then the raise
        assert hooks.calls == [
            ("before", "post", "webhook"),
            ("error", "post", "provider 429"),  # fired once, after exhaustion
        ]

    def test_on_fail_continue_absorbs_the_exhausted_failure(self):
        runner, calls = flaky_runner(fail_times=99, step_id="boom")
        steps = [{"id": "boom", "type": "webhook", "url": "https://example.test",
                  "autoretry": {"attempts": 1}, "on_fail": "continue"},
                 {"id": "post", "type": "webhook", "url": "https://example.test"}]
        with patch("src.dapier.engine.logic.time.sleep"):
            stop, hooks, outputs = run_chain(steps, run_action=runner)

        assert stop is None
        assert calls == ["boom", "boom", "post"]  # boom tried twice, then on
        assert outputs["boom"] == {"status": "skipped", "error": "provider 429",
                                   "output": {}}
        assert ("after", "post", "completed", {"ok": True}) in hooks.calls

    def test_on_error_continue_records_failed_and_moves_on(self):
        runner, calls = flaky_runner(fail_times=99, step_id="boom")
        steps = [{"id": "boom", "type": "webhook", "url": "https://example.test",
                  "autoretry": {"attempts": 1}, "on_error": "continue"},
                 {"id": "post", "type": "webhook", "url": "https://example.test"}]
        with patch("src.dapier.engine.logic.time.sleep"):
            stop, hooks, outputs = run_chain(steps, run_action=runner)

        assert stop is None
        assert calls == ["boom", "boom", "post"]
        assert outputs["boom"] == {"status": "failed", "error": "provider 429",
                                   "output": {}}

    def test_on_error_run_executes_the_error_branch_after_exhaustion(self):
        runner, calls = flaky_runner(fail_times=99, step_id="boom")
        steps = [{"id": "boom", "type": "webhook", "url": "https://example.test",
                  "autoretry": {"attempts": 1}, "on_error": "run",
                  "error_actions": [{"id": "alert", "type": "webhook",
                                     "url": "https://example.test/alert"}]}]
        with patch("src.dapier.engine.logic.time.sleep"):
            _stop, hooks, outputs = run_chain(steps, run_action=runner)

        assert ("before", "boom.error.alert", "webhook") in hooks.calls
        assert ("after", "boom", "failed", {}) in hooks.calls

    def test_exhausted_failure_is_not_recorded_as_success(self):
        # No `attempts` key leaks into the failed step's context: the error
        # policies keep their exact shapes (empty output).
        runner, _calls = flaky_runner(fail_times=99, step_id="boom")
        steps = [{"id": "boom", "type": "webhook", "url": "https://example.test",
                  "autoretry": {"attempts": 1}, "on_error": "continue"}]
        with patch("src.dapier.engine.logic.time.sleep"):
            _stop, _hooks, outputs = run_chain(steps, run_action=runner)
        assert outputs["boom"]["output"] == {}


class ConfigErrorTests(unittest.TestCase):
    """A bad autoretry config fails loudly before the step runs — it is an
    authoring error, not a failure on_fail could absorb."""

    def run_chain_with(self, step):
        runner, calls = flaky_runner(fail_times=0)
        hooks = Hooks()
        with pytest.raises(ValueError):
            run_chain([step], run_action=runner, hooks=hooks)
        assert calls == []  # the action never ran
        return hooks

    def test_bad_config_fails_even_with_on_fail_continue(self):
        for bad in ({"attempts": 9}, {"attempts": 0}, {"attempts": "2"},
                    {"initial_seconds": 0}, {"max_seconds": 61},
                    {"attempts": 1, "bogus": 1}, "fast", {}):
            with self.subTest(autoretry=bad):
                hooks = self.run_chain_with(
                    {"id": "post", "type": "webhook", "url": "https://example.test",
                     "autoretry": bad, "on_fail": "continue"})
                assert not any(call[0] == "error" for call in hooks.calls)

    def test_initial_above_max_is_rejected(self):
        self.run_chain_with(
            {"id": "post", "type": "webhook", "url": "https://example.test",
             "autoretry": {"attempts": 1, "initial_seconds": 30, "max_seconds": 10}})

    def test_logic_step_cannot_carry_autoretry(self):
        runner, calls = flaky_runner(fail_times=0)
        hooks = Hooks()
        with pytest.raises(ValueError, match="autoretry only applies to connector actions"):
            run_chain([{"id": "gate", "type": "filter", "field": "route",
                        "operator": "equals", "value": "invoice",
                        "autoretry": {"attempts": 1}}],
                      run_action=runner, hooks=hooks)
        assert calls == []

    def test_empty_and_absent_keys_are_not_opt_in(self):
        runner, calls = flaky_runner(fail_times=1)
        with pytest.raises(RuntimeError):
            run_chain([{"id": "post", "type": "webhook", "url": "https://example.test",
                        "autoretry": None}], run_action=runner)
        assert calls == ["post"]


class FullEngineTests(unittest.TestCase):
    """The real dispatch path: retries happen inside one run invocation."""

    def test_engine_retries_the_flaky_action(self):
        workflow = {
            "id": "wf-retry", "enabled": True,
            "trigger": {"connector": "email", "event": "message.received", "filters": {}},
            "actions": [{"id": "post", "type": "webhook", "url": "https://example.test",
                         "autoretry": {"attempts": 2}}],
        }
        calls = []

        def flaky(action, event, workflow_id=None, steps=None):
            calls.append(action.get("id"))
            if len(calls) < 2:
                raise RuntimeError("provider 429")
            return {"status": 200}

        hooks = Hooks()
        with patch("src.dapier.engine.all_workflows", return_value=[workflow]), \
                patch("src.dapier.connectors.registry.run_action", side_effect=flaky), \
                patch("src.dapier.engine.logic.time.sleep"):
            matched = execute({**EVENT}, before_action=hooks.before,
                              after_action=hooks.after, on_action_error=hooks.error)

        assert matched == ["wf-retry"]
        assert calls == ["post", "post"]
        assert ("after", "post", "completed", {"status": 200, "attempts": 2}) in hooks.calls


class DryrunTests(unittest.TestCase):
    """Autoretry is invisible to the dry-run/test-step surface: nothing there
    rejects the key (a dry-run renders inputs; it never runs the action)."""

    def test_dry_run_accepts_a_step_with_autoretry(self):
        from src.dapier.engine import dryrun

        workflow = {"id": "wf-dr", "enabled": True,
                    "trigger": {"connector": "email", "event": "message.received"},
                    "actions": [{"id": "post", "type": "webhook",
                                 "url": "https://example.test",
                                 "autoretry": {"attempts": 2}}]}
        report = dryrun.dry_run(workflow, {"subject": "hi"})
        assert report["steps"][0]["ok"] is True
        assert report["steps"][0]["rendered_input"]["autoretry"] == {"attempts": 2}


# --- save-time validation (registry layer; the engine shares the checks) ----


def webhook_chain(**extra):
    action = {"type": "webhook", "url": "https://example.test/hook"}
    action.update(extra)
    return [action]


class ValidationTests(unittest.TestCase):
    def validate(self, action):
        return registry.validate_action_chain([{"type": "webhook",
                                                "url": "https://example.test",
                                                **action}])

    def test_valid_configs_are_accepted(self):
        for config in ({"attempts": 1}, {"attempts": 3},
                       {"attempts": 2, "initial_seconds": 1},
                       {"attempts": 2, "max_seconds": 60},
                       {"attempts": 2, "initial_seconds": 2, "max_seconds": 60},
                       {"attempts": 2, "initial_seconds": 1.5, "max_seconds": 7.5}):
            with self.subTest(autoretry=config):
                chain = self.validate({"id": "post", "autoretry": config})
                assert chain[0]["autoretry"] == config

    def test_empty_string_reads_as_unset(self):
        chain = self.validate({"autoretry": ""})
        assert chain[0]["autoretry"] == ""

    def test_attempts_bounds(self):
        from src.dapier.connectors.registry import ActionError

        for bad in (0, -1, 4, 2.5, "2", True):
            with self.subTest(attempts=bad):
                with pytest.raises(ActionError,
                                   match="autoretry attempts must be a whole number"):
                    self.validate({"autoretry": {"attempts": bad}})
        for empty in (None,):
            with self.subTest(attempts=empty):
                with pytest.raises(ActionError, match="autoretry needs attempts"):
                    self.validate({"autoretry": {"attempts": empty}})
        with pytest.raises(ActionError, match="autoretry needs attempts"):
            self.validate({"autoretry": {"initial_seconds": 5}})

    def test_seconds_bounds(self):
        from src.dapier.connectors.registry import ActionError

        for key in ("initial_seconds", "max_seconds"):
            for bad in (0, -5, 61, "10", True):
                with self.subTest(key=key, value=bad):
                    with pytest.raises(ActionError, match=f"autoretry {key} must be a number"):
                        self.validate({"autoretry": {"attempts": 1, key: bad}})

    def test_initial_above_max_is_rejected(self):
        from src.dapier.connectors.registry import ActionError

        with pytest.raises(ActionError, match="initial_seconds must not exceed max_seconds"):
            self.validate({"autoretry": {"attempts": 1, "initial_seconds": 30,
                                         "max_seconds": 10}})

    def test_unknown_sub_keys_and_non_mapping_shapes(self):
        from src.dapier.connectors.registry import ActionError

        with pytest.raises(ActionError, match="autoretry has unknown keys: wait"):
            self.validate({"autoretry": {"attempts": 1, "wait": 5}})
        with pytest.raises(ActionError, match="autoretry must be a mapping"):
            self.validate({"autoretry": "fast"})
        with pytest.raises(ActionError, match="autoretry must be a mapping"):
            self.validate({"autoretry": ["attempts"]})
        with pytest.raises(ActionError, match="autoretry needs attempts"):
            self.validate({"autoretry": {}})

    def test_autoretry_on_a_logic_step_fails_the_save(self):
        from src.dapier.connectors.registry import ActionError

        with pytest.raises(ActionError, match="autoretry only applies to connector actions"):
            registry.validate_autoretry_key(
                {"type": "filter", "field": "route", "autoretry": {"attempts": 1}},
                "step 'gate'")

    def test_other_unknown_keys_are_still_rejected(self):
        from src.dapier.connectors.registry import ActionError

        with pytest.raises(ActionError, match="unknown keys: nope"):
            self.validate({"autoretry": {"attempts": 1}, "nope": 1})

    def test_bounds_and_engine_defaults_agree(self):
        """registry.AUTORETRY_SECONDS_BOUNDS bounds the engine's backoff
        defaults (deliberately duplicated literals — pin them together)."""
        low, high = registry.AUTORETRY_SECONDS_BOUNDS
        defaults = logic.AUTORETRY_DEFAULTS
        assert low <= defaults["initial_seconds"] <= high
        assert low <= defaults["max_seconds"] <= high
        assert registry.AUTORETRY_ATTEMPTS_MAX == 3
        assert set(registry.AUTORETRY_FIELDS) == \
            {"attempts", "initial_seconds", "max_seconds"}


if __name__ == "__main__":
    pytest.main([__file__])
