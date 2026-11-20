"""A redelivered event recovers a completed branching step from run history;
its branch children must come back under their nested ids too.

Regression for the 2026-11-19/20 dev-mailbox failures: on each queue
redelivery the ``route`` condition was restored whole, ``route.else.post-webhook``
never re-entered ``steps``, and verify-delivery failed the run as if the
webhook leg had not been delivered.
"""
from src.dapier.engine.logic_pkg import CompletedStep, run_chain


def _gate(records):
    """A worker-style step gate over recorded step statuses: completed steps
    are restored from history, anything else runs (True)."""
    def before_action(workflow_id, action_id, event, action_type=None):
        record = records.get(action_id)
        if record and record[0] in ("completed", "filtered"):
            return CompletedStep(*record)
        return True
    return before_action


ROUTE = {
    "id": "route", "type": "condition",
    "when": {"steps.decide.output.route": {"equals": "hub"}},
    "then": [{"id": "notify", "type": "http_request"}],
    "else": [{"id": "post-webhook", "type": "http_request"},
             {"id": "post-copy", "type": "http_request"}],
}


def test_recovered_condition_restores_completed_children_without_rerunning():
    records = {
        "route": ("completed", {"branch": "else", "condition": "failed"}),
        "route.else.post-webhook": ("completed", {"status": 202}),
        "route.else.post-copy": ("completed", {"status": 200}),
    }
    calls = []

    def dispatch(step, event, workflow_id, steps=None):
        calls.append(step["id"])
        return {"status": 500}

    outputs = {}
    run_chain("wf", [ROUTE], {"id": "evt", "data": {}}, dispatch,
              before_action=_gate(records), step_outputs=outputs)
    assert calls == []
    assert outputs["route"]["output"]["branch"] == "else"
    assert outputs["route.else.post-webhook"] == {"status": "completed",
                                                  "output": {"status": 202}}
    assert outputs["route.else.post-copy"]["status"] == "completed"
    assert "route.then.notify" not in outputs


def test_recovered_condition_reruns_unfinished_children_on_the_recorded_branch():
    # The predicate would now pick "then" (decide is absent), but the
    # recorded branch wins: the run stays on the path it already took.
    records = {
        "route": ("completed", {"branch": "else", "condition": "failed"}),
        "route.else.post-webhook": ("completed", {"status": 202}),
        "route.else.post-copy": ("failed", {}),
    }
    calls = []

    def dispatch(step, event, workflow_id, steps=None):
        calls.append(step["id"])
        return {"status": 200}

    outputs = {}
    run_chain("wf", [ROUTE], {"id": "evt", "data": {}}, dispatch,
              before_action=_gate(records), step_outputs=outputs)
    assert calls == ["post-copy"]
    assert outputs["route.else.post-copy"] == {"status": "completed",
                                               "output": {"status": 200}}


def test_recovered_paths_step_restores_its_recorded_path_children():
    step = {
        "id": "pick", "type": "paths",
        "paths": [{"label": "urgent", "when": {"subject": {"contains": "!"}},
                   "actions": [{"id": "page", "type": "http_request"}]}],
        "default": [{"id": "log", "type": "http_request"}],
    }
    records = {
        "pick": ("completed", {"branch": "urgent", "matched": "urgent"}),
        "pick.urgent.page": ("completed", {"status": 202}),
    }
    outputs = {}
    run_chain("wf", [step], {"id": "evt", "data": {}},
              lambda *args, **kwargs: {"status": 500},
              before_action=_gate(records), step_outputs=outputs)
    assert outputs["pick.urgent.page"]["output"] == {"status": 202}
    assert "pick.default.log" not in outputs


def test_recovered_condition_without_a_recorded_branch_runs_nothing():
    records = {"route": ("completed", {"result": {}})}
    calls = []
    outputs = {}
    run_chain("wf", [ROUTE], {"id": "evt", "data": {}},
              lambda step, *args, **kwargs: calls.append(step["id"]) or {},
              before_action=_gate(records), step_outputs=outputs)
    assert calls == []
    assert list(outputs) == ["route"]
