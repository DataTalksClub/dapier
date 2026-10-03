"""Per-step error handling: ``on_error: halt|continue|run`` + error_actions.

A step exception used to abort the run unconditionally; the generic
error-handling keys let a step absorb its failure (and expose it to
templating) or run a fallback chain in its place. Validation of the keys at
save time lives in tests/test_on_error_validation.py.

The delay step's real-delay behavior is covered here too: up to
MAX_DELAY_SECONDS the step sleeps inline; a longer pause (or ``until``)
raises ``RunSuspended`` carrying the remaining chain and accumulated
outputs — the worker parks that on the event queue (tests/test_worker.py).
"""
import time
import unittest
from datetime import datetime, timezone
from unittest import mock

import pytest

from conftest import fake_logic_time
from src.dapier.engine import logic
from src.dapier.engine.actions import templating


EVENT = {
    "id": "evt-1",
    "connector": "email",
    "event": "message.received",
    "data": {"route": "invoice", "subject": "Invoice 42"},
}


class Hooks:
    """Record the telemetry calls the chain makes, like the worker's ledger."""

    def __init__(self):
        self.calls = []

    def before(self, workflow_id, action_id, event, action_type):
        self.calls.append(("before", action_id, action_type))
        return True

    def after(self, workflow_id, action_id, event, **kwargs):
        self.calls.append(("after", action_id, kwargs.get("status"), kwargs.get("output")))

    def error(self, workflow_id, action_id, event, exc, **kwargs):
        self.calls.append(("error", action_id, str(exc)))


def run_chain(steps, *, data=None, run_action=None, hooks=None, step_outputs=None):
    hooks = hooks or Hooks()
    outputs = {} if step_outputs is None else step_outputs
    event = {**EVENT, "data": EVENT["data"] if data is None else data}
    stop = logic.run_chain(
        "wf-1", steps, event,
        run_action or (lambda action, event, workflow_id, steps=None: {"ok": True}),
        before_action=hooks.before, after_action=hooks.after,
        on_action_error=hooks.error, step_outputs=outputs,
    )
    return stop, hooks, outputs


def failing_runner(fail, calls, render_text=None):
    """A runner that raises for the named step ids and records the rest.

    ``fail`` maps step id -> exception message; ``render_text`` (a dict)
    collects the rendered ``text`` field of steps that carry one, keyed by id.
    """

    def run(action, event, workflow_id, steps=None):
        calls.append(action["id"])
        if action["id"] in fail:
            raise RuntimeError(fail[action["id"]])
        if render_text is not None and "text" in action:
            render_text[action["id"]] = templating.render(action["text"], event, steps)
        return {"ok": True}

    return run


class HaltTests(unittest.TestCase):
    def test_halt_is_the_default_and_raises(self):
        calls = []
        outputs = {}
        hooks = Hooks()
        with pytest.raises(RuntimeError, match="kaboom"):
            run_chain(
                [
                    {"id": "boom", "type": "webhook", "url": "https://example.test"},
                    {"id": "after", "type": "webhook", "url": "https://example.test"},
                ],
                run_action=failing_runner({"boom": "kaboom"}, calls), hooks=hooks,
                step_outputs=outputs,
            )

        assert calls == ["boom"]  # the chain never reached the next step
        assert ("before", "boom", "webhook") in hooks.calls
        assert ("error", "boom", "kaboom") in hooks.calls
        assert not any(call[0] == "after" and call[1] == "boom" for call in hooks.calls)
        assert "boom" not in outputs  # a halted step leaves no output behind

    def test_on_error_halt_is_explicitly_the_same(self):
        calls = []
        with pytest.raises(RuntimeError, match="kaboom"):
            run_chain(
                [{"id": "boom", "type": "webhook", "url": "https://example.test",
                  "on_error": "halt"}],
                run_action=failing_runner({"boom": "kaboom"}, calls),
            )
        assert calls == ["boom"]


class ContinueTests(unittest.TestCase):
    def test_continue_records_failed_and_proceeds(self):
        calls = []
        outputs = {}
        hooks = Hooks()
        stop, hooks, outputs = run_chain(
            [
                {"id": "boom", "type": "webhook", "url": "https://example.test",
                 "on_error": "continue"},
                {"id": "after", "type": "webhook", "url": "https://example.test"},
            ],
            run_action=failing_runner({"boom": "kaboom"}, calls), hooks=hooks,
            step_outputs=outputs,
        )

        assert stop is None
        assert calls == ["boom", "after"]
        assert outputs["boom"] == {"status": "failed", "error": "kaboom", "output": {}}
        assert ("error", "boom", "kaboom") in hooks.calls
        assert ("after", "boom", "failed", {}) in hooks.calls
        assert ("after", "after", "completed", {"ok": True}) in hooks.calls

    def test_next_step_templates_the_error_and_status(self):
        calls = []
        rendered = {}
        stop, _hooks, _outputs = run_chain(
            [
                {"id": "boom", "type": "webhook", "url": "https://example.test",
                 "on_error": "continue"},
                {"id": "note", "type": "webhook", "url": "https://example.test",
                 "text": "{steps.boom.status}: {steps.boom.error}"},
            ],
            run_action=failing_runner({"boom": "kaboom"}, calls, rendered),
        )

        assert stop is None
        assert calls == ["boom", "note"]
        assert rendered["note"] == "failed: kaboom"

    def test_continue_inside_a_loop_lets_other_items_run(self):
        calls = []
        stop, hooks, _outputs = run_chain(
            [{"id": "each", "type": "for_each", "list": "items",
              "actions": [
                  {"id": "boom", "type": "webhook", "url": "https://example.test",
                   "on_error": "continue"},
                  {"id": "marker", "type": "webhook", "url": "https://example.test"},
              ]}],
            data={"items": ["a", "b"]},
            run_action=failing_runner({"boom": "kaboom"}, calls),
        )

        assert stop is None
        assert calls == ["boom", "marker", "boom", "marker"]
        assert ("after", "each", "completed",
                {"iterated": 2, "items": 2, "skipped": 0, "truncated": False}) in hooks.calls


class RunErrorActionsTests(unittest.TestCase):
    def test_run_executes_error_actions_with_prefix_and_proceeds(self):
        calls = []
        rendered = {}
        hooks = Hooks()

        def runner(action, event, workflow_id, steps=None):
            calls.append(action["id"])
            if action["id"] == "boom":
                raise RuntimeError("kaboom")
            if "text" in action:
                rendered[action["id"]] = templating.render(action["text"], event, steps)
            return {"ok": True}

        stop, hooks, outputs = run_chain(
            [
                {"id": "boom", "type": "webhook", "url": "https://example.test",
                 "on_error": "run",
                 "error_actions": [
                     {"id": "alert", "type": "webhook", "url": "https://example.test",
                      "text": "step failed: {steps.boom.error}"},
                 ]},
                {"id": "after", "type": "webhook", "url": "https://example.test"},
            ],
            run_action=runner, hooks=hooks,
        )

        assert stop is None
        assert calls == ["boom", "alert", "after"]
        assert ("before", "boom.error.alert", "webhook") in hooks.calls
        assert ("after", "boom.error.alert", "completed", {"ok": True}) in hooks.calls
        assert rendered["alert"] == "step failed: kaboom"
        assert outputs["boom"]["status"] == "failed"
        assert outputs["boom.error.alert"] == {"status": "completed", "output": {"ok": True}}

    def test_filter_inside_the_error_branch_stops_quietly(self):
        calls = []
        stop, hooks, _outputs = run_chain(
            [
                {"id": "boom", "type": "webhook", "url": "https://example.test",
                 "on_error": "run",
                 "error_actions": [
                     {"id": "gate", "type": "filter", "field": "route",
                      "operator": "equals", "value": "receipt"},
                 ]},
                {"id": "after", "type": "webhook", "url": "https://example.test"},
            ],
            run_action=failing_runner({"boom": "kaboom"}, calls),
        )

        assert stop == "filtered"
        assert calls == ["boom"]
        assert not any(call[0] == "after" and call[1] == "after" for call in hooks.calls)

    def test_a_failing_error_branch_halts_the_run(self):
        calls = []
        with pytest.raises(RuntimeError, match="alert down"):
            run_chain(
                [{"id": "boom", "type": "webhook", "url": "https://example.test",
                  "on_error": "run",
                  "error_actions": [
                      {"id": "alert", "type": "webhook", "url": "https://example.test"},
                  ]}],
                run_action=failing_runner({"boom": "kaboom", "alert": "alert down"}, calls),
            )
        assert calls == ["boom", "alert"]


class ConfigErrorTests(unittest.TestCase):
    def test_invalid_mode_is_a_config_error(self):
        calls = []
        with pytest.raises(ValueError, match="on_error must be one of"):
            run_chain(
                [{"id": "boom", "type": "webhook", "url": "https://example.test",
                  "on_error": "explode"}],
                run_action=failing_runner({"boom": "kaboom"}, calls),
            )

    def test_run_without_error_actions_is_a_config_error(self):
        calls = []
        with pytest.raises(ValueError, match="non-empty error_actions list"):
            run_chain(
                [{"id": "boom", "type": "webhook", "url": "https://example.test",
                  "on_error": "run"}],
                run_action=failing_runner({"boom": "kaboom"}, calls),
            )


class ExecuteIntegrationTests(unittest.TestCase):
    """The full engine path: a handled failure keeps the run going."""

    def run_workflow(self, actions):
        from unittest.mock import patch

        from conftest import stubbed_action

        workflow = {
            "id": "wf-err", "enabled": True,
            "trigger": {"connector": "email", "event": "message.received", "filters": {}},
            "actions": actions,
        }
        hooks = Hooks()
        calls = []

        def runner(action, event):
            calls.append(action["id"])
            if action["id"] == "boom":
                raise RuntimeError("kaboom")
            return {"status": 200}

        with patch("src.dapier.engine.all_workflows", return_value=[workflow]), \
                stubbed_action("webhook", runner):
            from src.dapier.engine import execute
            execute({**EVENT}, before_action=hooks.before, after_action=hooks.after,
                    on_action_error=hooks.error)
        return calls, hooks

    def test_continue_runs_the_rest_of_the_workflow(self):
        calls, hooks = self.run_workflow([
            {"id": "boom", "type": "webhook", "url": "https://example.test",
             "on_error": "continue"},
            {"id": "post", "type": "webhook", "url": "https://example.test"},
        ])

        assert calls == ["boom", "post"]
        assert ("after", "boom", "failed", {}) in hooks.calls
        assert ("after", "post", "completed", {"status": 200}) in hooks.calls


def _iso_in(seconds):
    return datetime.fromtimestamp(time.time() + seconds, tz=timezone.utc).isoformat()


class _Clock:
    """Stand-in for logic.time: records sleeps but keeps the real clock."""

    def __init__(self):
        self.slept = []

    def sleep(self, seconds):
        self.slept.append(seconds)

    def time(self):
        return time.time()

    def monotonic(self):
        return time.monotonic()


def run_delay(steps, data=None, run_action=None):
    """Run a chain with the clock faked; returns (clock, hooks, outputs, susp).

    ``susp`` is the raised ``RunSuspended`` or None when the chain completed.
    """
    clock = _Clock()
    hooks = Hooks()
    outputs = {}
    event = {**EVENT, "data": EVENT["data"] if data is None else data}
    with fake_logic_time(clock):
        try:
            logic.run_chain(
                "wf-1", steps, event,
                run_action or (lambda action, event, workflow_id, steps=None: {"ok": True}),
                before_action=hooks.before, after_action=hooks.after,
                on_action_error=hooks.error, step_outputs=outputs,
            )
            susp = None
        except logic.RunSuspended as raised:
            susp = raised
    return clock, hooks, outputs, susp


class DelayInlineTests(unittest.TestCase):
    """Delays at or under the 60 s cap keep sleeping inside the invocation."""

    def test_delay_within_the_cap_sleeps_inline(self):
        clock, hooks, outputs, susp = run_delay(
            [{"id": "pause", "type": "delay", "seconds": 2}])

        assert susp is None
        assert clock.slept == [2.0]
        assert outputs["pause"] == {
            "status": "completed",
            "output": {"delay_seconds": 2.0, "slept_seconds": 2.0},
        }
        assert ("after", "pause", "completed",
                {"delay_seconds": 2.0, "slept_seconds": 2.0}) in hooks.calls

    def test_combined_minutes_and_seconds_sleep_inline(self):
        clock, _hooks, _outputs, susp = run_delay(
            [{"id": "pause", "type": "delay", "minutes": 0.5, "seconds": 1}])

        assert susp is None
        assert clock.slept == [31.0]

    def test_until_a_moment_within_the_cap_sleeps_inline(self):
        clock, _hooks, outputs, susp = run_delay(
            [{"id": "pause", "type": "delay", "until": _iso_in(3)}])

        assert susp is None
        assert clock.slept == [pytest.approx(3, abs=1)]
        assert outputs["pause"]["status"] == "completed"

    def test_until_in_the_past_runs_immediately(self):
        clock, _hooks, outputs, susp = run_delay(
            [{"id": "pause", "type": "delay", "until": _iso_in(-30)}])

        assert susp is None
        assert clock.slept == [0.0]
        assert outputs["pause"]["status"] == "completed"
        assert outputs["pause"]["output"]["delay_seconds"] == 0


class DelaySuspendTests(unittest.TestCase):
    """A longer pause suspends the run: no sleep, a continuation instead."""

    def test_delay_past_the_cap_suspends_without_sleeping(self):
        clock, hooks, outputs, susp = run_delay(
            [{"id": "pause", "type": "delay", "seconds": logic.MAX_DELAY_SECONDS + 1}])

        assert clock.slept == []
        assert isinstance(susp, logic.RunSuspended)
        assert susp.resume_at == pytest.approx(time.time() + logic.MAX_DELAY_SECONDS + 1, abs=5)
        assert susp.output["suspended"] is True
        assert susp.output["delay_seconds"] == logic.MAX_DELAY_SECONDS + 1
        # The parked step closes out ``delayed`` so run history shows the pause.
        assert outputs["pause"]["status"] == "delayed"
        assert ("after", "pause", "delayed", susp.output) in hooks.calls

    def test_suspension_carries_the_remaining_chain_and_outputs(self):
        calls = []

        def runner(action, event, workflow_id, steps=None):
            calls.append(action["id"])
            return {"ok": True}

        _clock, _hooks, outputs, susp = run_delay(
            [{"id": "pause", "type": "delay", "seconds": 3600},
             {"id": "after", "type": "webhook", "url": "https://example.test"}],
            run_action=runner)

        assert calls == []  # nothing after the pause ran
        assert susp.segments == [{
            "steps": [{"id": "after", "type": "webhook", "url": "https://example.test"}],
            "prefix": "", "scope": None,
        }]
        assert susp.step_outputs["pause"]["status"] == "delayed"
        assert "after" not in outputs

        # Resuming replays only the remainder, with the earlier outputs kept.
        stop = logic.resume_chain(
            "wf-1", susp.segments, {**EVENT}, runner, step_outputs=susp.step_outputs)
        assert stop is None
        assert calls == ["after"]

    def test_suspension_inside_a_loop_resumes_the_remaining_iterations(self):
        calls = []

        def runner(action, event, workflow_id, steps=None):
            calls.append(action["id"])
            return {"ok": True}

        items = [{"wait": 3600}, {"wait": 2}]  # only the first item pauses long
        _clock, hooks, _outputs, susp = run_delay(
            [{"id": "each", "type": "for_each", "list": "items",
              "actions": [
                  {"id": "pause", "type": "delay", "seconds": "{item.wait}"},
                  {"id": "post", "type": "webhook", "url": "https://example.test"},
              ]}],
            data={"items": items}, run_action=runner)

        assert calls == []
        # Innermost first: the paused iteration's remainder, then the loop's
        # remaining iterations, so the branch resumes before the outer tail.
        assert "steps" in susp.segments[0] and "loop" in susp.segments[1]
        loop = susp.segments[1]["loop"]
        assert loop["from"] == 1

        resumed = Hooks()
        with fake_logic_time(_Clock()):
            stop = logic.resume_chain(
                "wf-1", susp.segments, {**EVENT, "data": {"items": items}},
                runner, before_action=resumed.before, after_action=resumed.after,
                step_outputs=susp.step_outputs)
        assert stop is None
        assert calls == ["post", "post"]  # the paused tail, then iteration 1
        assert ("before", "each[1].post", "webhook") in resumed.calls


class DelayUntilTests(unittest.TestCase):
    """``until`` parsing: future suspends (or sleeps), past runs now, bad input refuses."""

    def test_far_future_until_suspends(self):
        _clock, _hooks, _outputs, susp = run_delay(
            [{"id": "pause", "type": "delay", "until": _iso_in(3600)}])

        assert isinstance(susp, logic.RunSuspended)
        assert susp.resume_at == pytest.approx(time.time() + 3600, abs=5)

    def test_until_renders_from_the_event_data(self):
        _clock, _hooks, _outputs, susp = run_delay(
            [{"id": "pause", "type": "delay", "until": "{resume_at}"}],
            data={"resume_at": _iso_in(3600)})

        assert isinstance(susp, logic.RunSuspended)
        assert susp.resume_at == pytest.approx(time.time() + 3600, abs=5)

    def test_invalid_until_is_a_config_error(self):
        with pytest.raises(ValueError, match="until must be an ISO 8601"):
            run_delay([{"id": "pause", "type": "delay", "until": "not-a-date"}])

    def test_until_and_a_duration_are_mutually_exclusive(self):
        with pytest.raises(ValueError, match="not both"):
            run_delay([{"id": "pause", "type": "delay",
                        "until": _iso_in(10), "seconds": 5}])

    def test_until_beyond_the_retention_ceiling_is_refused(self):
        with pytest.raises(ValueError, match="may not exceed 90 days"):
            run_delay([{"id": "pause", "type": "delay", "until": _iso_in(91 * 86400)}])

    def test_days_beyond_the_retention_ceiling_are_refused(self):
        with pytest.raises(ValueError, match="may not exceed 90 days"):
            run_delay([{"id": "pause", "type": "delay", "days": 91}])

    def test_a_delay_needs_something_to_wait_for(self):
        with pytest.raises(ValueError, match="needs seconds"):
            run_delay([{"id": "pause", "type": "delay"}])

    def test_template_seconds_render_from_the_steps_context(self):
        clock, _hooks, _outputs, susp = run_delay(
            [{"id": "wait", "type": "webhook", "url": "https://example.test"},
             {"id": "pause", "type": "delay", "seconds": "{steps.wait.output.seconds}"}],
            run_action=lambda action, event, workflow_id, steps=None:
                {"seconds": "61"} if action["id"] == "wait" else {"ok": True})

        assert isinstance(susp, logic.RunSuspended)  # rendered to 61 > cap
        assert clock.slept == []


if __name__ == "__main__":
    pytest.main([__file__])
