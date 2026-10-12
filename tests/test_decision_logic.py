"""In-workflow logic step: decision — the LLM smart if/switch.

One structured completion (the ai_complete machinery, so the same LLM_* env —
LLM_BASE_URL at OpenRouter makes it an OpenRouter decision) picks a branch:
``mode: if`` answers the prompt's yes/no question and runs then/else;
``mode: switch`` picks one of ``choices`` and runs the matching case's
actions, falling back to ``default_actions`` on an unusable or off-list
reply. The save-time validator, the dry-run and the engine agree on the
step's shape, and the step output carries the choice for later steps.
"""
import json
import os
import unittest
from unittest import mock

import pytest

from src.dapier.engine import dryrun, logic


EVENT = {
    "id": "evt-1",
    "connector": "zoom",
    "event": "recording.completed",
    "data": {"topic": "Weekly sync with Bob", "meeting_id": "123",
             "host_email": "alexey@datatalks.club"},
}


def llm_transport(content, model="test-model"):
    """A transport seam answering one fixed assistant message."""
    def transport(method, url, *, headers, body, timeout):
        assert url.endswith("/chat/completions")
        assert headers["authorization"] == "Bearer test-key"
        return 200, json.dumps({
            "model": model,
            "choices": [{"message": {"content": content}}],
            "usage": {"total_tokens": 7},
        }).encode()
    return transport


def default_transport_for(content):
    """ai.py's positional _default_transport seam wrapped around llm_transport."""
    seam = llm_transport(content)

    def default_transport(url, payload, headers, timeout):
        return seam("POST", url, headers=headers, body=payload, timeout=timeout)

    return default_transport


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


def run_decision(step, content, *, event=None, run_action=None, hooks=None):
    hooks = hooks or Hooks()
    ran = []
    runner = run_action or (lambda action, event, workflow_id, steps=None:
                            ran.append(action["id"]) or {})
    with mock.patch.dict(os.environ, {"LLM_API_KEY": "test-key"}):
        stop, output, status = logic._run_decision(
            "wf-1", str(step["id"]), step, event or EVENT, runner,
            before_action=hooks.before, after_action=hooks.after,
            on_action_error=hooks.error, step_outputs={},
            transport=llm_transport(content))
    return stop, output, status, ran, hooks


SWITCH_STEP = {
    "id": "classify",
    "type": "decision",
    "mode": "switch",
    "prompt": "Classify this recording. Topic: {topic}",
    "choices": ["one-on-one", "ai-shipping-labs"],
    "cases": [
        {"choice": "one-on-one",
         "actions": [{"id": "zoom-calls-agent", "type": "agent", "prompt": "x"}]},
        {"choice": "ai-shipping-labs",
         "actions": [{"id": "aisl-agent", "type": "agent", "prompt": "y"}]},
    ],
    "default_actions": [{"id": "fallback", "type": "agent", "prompt": "z"}],
}


class SwitchTests(unittest.TestCase):
    def test_matching_case_runs(self):
        stop, output, status, ran, hooks = run_decision(
            SWITCH_STEP, json.dumps({"choice": "one-on-one"}))
        assert stop is None and status == "completed"
        assert ran == ["zoom-calls-agent"]
        assert output["choice"] == "one-on-one"
        assert output["branch"] == "one-on-one"
        assert output["model"] == "test-model"
        assert output["usage"] == {"total_tokens": 7}
        # the branch nests under the step, like a condition's then/else
        assert ("before", "classify.one-on-one.zoom-calls-agent", "agent") in hooks.calls

    def test_request_carries_enum_and_zero_temperature(self):
        seen = {}

        def transport(method, url, *, headers, body, timeout):
            seen.update(json.loads(body))
            return 200, json.dumps({
                "model": "m",
                "choices": [{"message": {"content": '{"choice": "ai-shipping-labs"}'}}],
                "usage": {}}).encode()

        with mock.patch.dict(os.environ, {"LLM_API_KEY": "test-key"}):
            logic._run_decision(
                "wf-1", "classify", SWITCH_STEP, EVENT,
                lambda action, event, workflow_id, steps=None: {},
                before_action=None, after_action=None, on_action_error=None,
                step_outputs={}, transport=transport)
        schema = seen["response_format"]["json_schema"]["schema"]
        assert schema["properties"]["choice"]["enum"] == ["one-on-one", "ai-shipping-labs"]
        assert seen["temperature"] == 0.0

    def test_off_list_answer_runs_default(self):
        stop, output, status, ran, _ = run_decision(
            SWITCH_STEP, json.dumps({"choice": "webinar"}))
        assert ran == ["fallback"]
        assert output["branch"] == "default"
        assert "not one of the allowed values" in output["note"]

    def test_off_list_answer_without_default_fails(self):
        step = {k: v for k, v in SWITCH_STEP.items() if k != "default_actions"}
        with pytest.raises(ValueError, match="not one of"):
            run_decision(step, json.dumps({"choice": "webinar"}))

    def test_unparsable_reply_runs_default(self):
        stop, output, status, ran, _ = run_decision(SWITCH_STEP, "not json at all")
        assert ran == ["fallback"]
        assert output["branch"] == "default"

    def test_unparsable_reply_without_default_fails(self):
        step = {k: v for k, v in SWITCH_STEP.items() if k != "default_actions"}
        with pytest.raises(ValueError, match="could not parse"):
            run_decision(step, "not json at all")

    def test_mode_defaults_to_switch_when_choices_present(self):
        step = {k: v for k, v in SWITCH_STEP.items() if k != "mode"}
        stop, output, status, ran, _ = run_decision(
            step, json.dumps({"choice": "ai-shipping-labs"}))
        assert output["mode"] == "switch"
        assert ran == ["aisl-agent"]


IF_STEP = {
    "id": "ask",
    "type": "decision",
    "mode": "if",
    "prompt": "Is {topic} a one-on-one call?",
    "then": [{"id": "yes", "type": "agent", "prompt": "x"}],
    "else": [{"id": "no", "type": "agent", "prompt": "y"}],
}


class IfTests(unittest.TestCase):
    def test_true_runs_then(self):
        stop, output, status, ran, hooks = run_decision(
            IF_STEP, json.dumps({"answer": True}))
        assert ran == ["yes"]
        assert output["branch"] == "then"
        assert output["choice"] is True
        assert ("before", "ask.then.yes", "agent") in hooks.calls

    def test_false_runs_else(self):
        stop, output, status, ran, hooks = run_decision(
            IF_STEP, json.dumps({"answer": False}))
        assert ran == ["no"]
        assert output["branch"] == "else"
        assert output["choice"] is False
        assert ("before", "ask.else.no", "agent") in hooks.calls

    def test_non_boolean_answer_fails(self):
        with pytest.raises(ValueError, match="should be boolean"):
            run_decision(IF_STEP, json.dumps({"answer": "maybe"}))

    def test_mode_defaults_to_if_when_no_choices(self):
        step = {k: v for k, v in IF_STEP.items() if k != "mode"}
        stop, output, status, ran, _ = run_decision(
            step, json.dumps({"answer": True}))
        assert output["mode"] == "if"
        assert ran == ["yes"]


class ConfigTests(unittest.TestCase):
    def test_unconfigured_key_fails_with_setup_message(self):
        from src.dapier.connectors import ai

        with mock.patch.dict(os.environ, {ai.API_KEY_ENV: ""}):
            with pytest.raises(ValueError, match=ai.API_KEY_ENV):
                logic._run_decision(
                    "wf-1", "classify", SWITCH_STEP, EVENT,
                    lambda action, event, workflow_id, steps=None: {},
                    before_action=None, after_action=None,
                    on_action_error=None, step_outputs={},
                    transport=llm_transport(json.dumps({"choice": "one-on-one"})))

    def test_missing_prompt_fails_before_any_call(self):
        step = {k: v for k, v in SWITCH_STEP.items() if k != "prompt"}
        with pytest.raises(ValueError, match="needs a prompt"):
            run_decision(step, json.dumps({"choice": "one-on-one"}))

    def test_unknown_mode_fails(self):
        step = {**SWITCH_STEP, "mode": "maybe"}
        with pytest.raises(ValueError, match="mode must be one of"):
            run_decision(step, json.dumps({"choice": "one-on-one"}))

    def test_choice_reaches_downstream_steps(self):
        seen = {}

        def runner(action, event, workflow_id, steps=None):
            seen.update(steps or {})
            return {}

        with mock.patch.dict(os.environ, {"LLM_API_KEY": "test-key"}), \
                mock.patch("src.dapier.connectors.ai._default_transport",
                           default_transport_for(json.dumps({"choice": "one-on-one"}))):
            logic.run_chain(
                "wf-1", [SWITCH_STEP,
                         {"id": "after", "type": "webhook", "url": "https://example.test"}],
                EVENT, runner, before_action=None, after_action=None,
                on_action_error=None)
        assert seen["classify"]["output"]["choice"] == "one-on-one"


class EngineChainTests(unittest.TestCase):
    """The full path: run_chain dispatches decision like any logic step."""

    def test_run_chain_dispatches_decision(self):
        with mock.patch("src.dapier.connectors.ai._default_transport",
                        default_transport_for(json.dumps({"choice": "one-on-one"}))):
            with mock.patch.dict(os.environ, {"LLM_API_KEY": "test-key"}):
                ran, hooks = [], Hooks()
                stop = logic.run_chain(
                    "wf-1", [SWITCH_STEP], EVENT,
                    lambda action, event, workflow_id, steps=None:
                        ran.append(action["id"]) or {},
                    before_action=hooks.before, after_action=hooks.after,
                    on_action_error=hooks.error)
        assert stop is None
        assert ran == ["zoom-calls-agent"]


WORKFLOW = {
    "id": "zoom-recording-router",
    "trigger": {"connector": "zoom", "event": "recording.completed"},
    "actions": [SWITCH_STEP],
}


class ValidationTests(unittest.TestCase):
    """The save-time validator agrees with the engine on the step's shape."""

    def _parse(self, actions):
        from src.dapier.api import designer_store

        return designer_store.parse_workflow(json.dumps({**WORKFLOW, "actions": actions}))

    def test_valid_switch_parses(self):
        workflow = self._parse([SWITCH_STEP])
        assert workflow["actions"][0]["type"] == "decision"

    def test_valid_if_parses(self):
        workflow = self._parse([IF_STEP])
        assert workflow["actions"][0]["type"] == "decision"

    def test_missing_prompt_rejected(self):
        step = {k: v for k, v in SWITCH_STEP.items() if k != "prompt"}
        with pytest.raises(Exception, match="decision needs a prompt"):
            self._parse([step])

    def test_bad_mode_rejected(self):
        with pytest.raises(Exception, match="mode must be one of"):
            self._parse([{**SWITCH_STEP, "mode": "maybe"}])

    def test_empty_choices_rejected(self):
        with pytest.raises(Exception, match="non-empty list of choices"):
            self._parse([{**SWITCH_STEP, "choices": []}])

    def test_case_outside_choices_rejected(self):
        step = {**SWITCH_STEP,
                "cases": [{"choice": "webinar", "actions": []}]}
        with pytest.raises(Exception, match="not one of the step's choices"):
            self._parse([step])

    def test_duplicate_cases_rejected(self):
        step = {**SWITCH_STEP,
                "cases": [{"choice": "one-on-one", "actions": []},
                          {"choice": "one-on-one", "actions": []}]}
        with pytest.raises(Exception, match="duplicate case"):
            self._parse([step])

    def test_case_actions_must_be_steps(self):
        step = {**SWITCH_STEP,
                "cases": [{"choice": "one-on-one", "actions": "nope"}]}
        with pytest.raises(Exception, match="must be a list of steps"):
            self._parse([step])

    def test_if_branches_must_be_step_lists(self):
        with pytest.raises(Exception, match="decision then must be a list"):
            self._parse([{**IF_STEP, "then": "nope"}])

    def test_nested_steps_validate_too(self):
        step = {**SWITCH_STEP,
                "cases": [{"choice": "one-on-one",
                           "actions": [{"id": "bad-loop", "type": "for_each"}]}]}
        with pytest.raises(Exception, match="for_each needs a list field"):
            self._parse([step])

    def test_templated_temperature_skips_literal_check(self):
        workflow = self._parse([{**SWITCH_STEP, "temperature": "{settings.temp}"}])
        assert workflow["actions"][0]["temperature"] == "{settings.temp}"


class DryRunTests(unittest.TestCase):
    """Per-step testing evaluates the decision without consulting the LLM;
    a plain dry-run passes logic steps through; an execute test-run is the
    real engine (key + default transport patched here)."""

    def test_step_test_evaluates_without_the_llm(self):
        result = dryrun.test_step(WORKFLOW, "classify", EVENT)
        entry = result["steps"][0]
        assert entry["ok"] is True
        assert entry["output"]["note"] == "LLM not consulted — branch not executed"
        assert entry["output"]["mode"] == "switch"

    def test_step_test_rejects_a_bad_step(self):
        bad = {**WORKFLOW, "actions": [{k: v for k, v in SWITCH_STEP.items()
                                        if k != "choices"}]}
        result = dryrun.test_step(bad, "classify", EVENT)
        entry = result["steps"][0]
        assert entry["ok"] is False
        assert "choices" in entry["error"]

    def test_dry_run_passes_logic_steps_through(self):
        result = dryrun.test_run(WORKFLOW, EVENT)
        entry = next(s for s in result["steps"] if s["action_id"] == "classify")
        assert entry["ok"] is True

    def test_execute_test_run_routes_through_the_real_engine(self):
        # side-effect-free branch bodies: the agent steps would need the
        # host-task table, which a test-run does not carry
        workflow = {**WORKFLOW, "actions": [{**SWITCH_STEP, "cases": [
            {"choice": "one-on-one",
             "actions": [{"id": "mark", "type": "filter", "field": "topic",
                          "operator": "exists"}]},
            {"choice": "ai-shipping-labs", "actions": []},
        ]}]}
        with mock.patch.dict(os.environ, {"LLM_API_KEY": "test-key"}), \
                mock.patch("src.dapier.connectors.ai._default_transport",
                           default_transport_for(json.dumps({"choice": "one-on-one"}))):
            result = dryrun.test_run(workflow, EVENT, execute=True)
        entry = next(s for s in result["steps"] if s["action_id"] == "classify")
        assert entry["ok"] is True
        assert entry["output"]["choice"] == "one-on-one"

    def test_branch_steps_are_individually_addressable(self):
        step = dryrun.find_step(WORKFLOW, "classify.one-on-one.zoom-calls-agent")
        assert step is not None and step["type"] == "agent"
        assert dryrun.find_step(WORKFLOW, "classify.default.fallback") is not None
