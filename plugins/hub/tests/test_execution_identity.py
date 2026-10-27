"""Hub retries must use the same qualified step identity as run history."""
from src.dapier.engine import logic
from src.dapier.engine.logic_pkg.execution import current_action_id
from plugins.hub.session import request_key
import pytest


def test_loop_and_branch_dispatch_preserves_distinct_step_identity():
    calls = []

    def run_action(step, event, workflow_id, steps=None):
        assert step["id"] == "delegate"
        calls.append((current_action_id(), request_key(workflow_id, event["id"], current_action_id())))
        return {}

    inner = {"id": "delegate", "type": "hub_session", "title": "Task", "text": "Task",
             "acceptance_criteria": "Deliver"}
    chain = [{"id": "each", "type": "for_each", "list": "items", "actions": [inner]}]
    event = {"id": "event", "data": {"items": ["first", "second"]}}
    logic.run_chain("flow", chain, event, run_action)
    assert len(calls) == 2
    assert calls[0][0] != calls[1][0]
    assert calls[0][1] != calls[1][1]
    first = list(calls)
    calls.clear()
    logic.run_chain("flow", chain, event, run_action)
    assert calls == first
    assert inner["id"] == "delegate"  # workflow definitions are not mutated
    assert current_action_id() is None


def test_dispatch_failure_restores_context():
    def run_action(*args, **kwargs):
        assert current_action_id() == "branch.delegate"
        raise TimeoutError("lost response")
    with pytest.raises(TimeoutError):
        logic.run_chain("flow", [{"id": "delegate", "type": "hub_session"}],
                        {"id": "event", "data": {}}, run_action, prefix="branch")
    assert current_action_id() is None


def test_autoretry_reuses_qualified_identity(monkeypatch):
    calls = []
    monkeypatch.setattr("src.dapier.engine.logic_pkg.execution.time.sleep", lambda delay: None)

    def run_action(step, event, workflow_id, steps=None):
        calls.append(current_action_id())
        if len(calls) == 1:
            raise TimeoutError("ambiguous response")
        return {}
    logic.run_chain("flow", [{"id": "delegate", "type": "hub_session", "autoretry": {"attempts": 1}}],
                    {"id": "event", "data": {}}, run_action, prefix="branch")
    assert calls == ["branch.delegate", "branch.delegate"]
    assert current_action_id() is None
