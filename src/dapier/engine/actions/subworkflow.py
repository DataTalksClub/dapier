"""run_workflow action: run another published workflow in-process.

One workflow calling another ("run another zap"). The step looks its target
up the same way the engine's execute path does — ``matching.all_workflows``,
the bundle with the published-workflows overlay merged over it — builds a
synthetic event envelope shaped like the test-run path's
(:func:`dryrun.normalize_sample_event`), and executes the target's chain
through the real engine restricted to that one workflow: the same machinery
as ``dryrun.execute_once`` (forced trigger matching, no execution-record
writes, a failing step aborts the sub-run like a real one).

Depth and cycles: the current depth and the chain of ancestor workflow ids
travel in a ``ContextVar`` — the run context every step of a sub-run sees.
(A depth counter on ``logic.run_chain`` cannot reach this runner: the
connector-registry dispatch fixes the runner signature at
``(action, event, workflow_id, *, steps)`` for every registered action, so
the run context is the one channel that threads without breaking the
dispatch contract.) A step may only start a sub-run while fewer than
:data:`MAX_SUBWORKFLOW_DEPTH` sub-runs are already in flight — A→B→C runs,
A→B→C→D is rejected — and a target that is already running anywhere up the
chain (itself included) is rejected as a cycle before it executes.

Output: the step returns the target's step outputs under ``steps`` so parent
templating reads ``{steps.<id>.output.steps.<child-step>.<field>}``; with
``output_field`` naming one child step, its output sits under ``output``
(``{steps.<id>.output.output.<field>}``).
"""

import json
import uuid
from contextvars import ContextVar

from .. import matching
from . import templating

# How many run_workflow hops may be in flight at once. The workflow that owns
# the trigger runs at depth 0; its run_workflow steps run targets at depth 1,
# those run theirs at depth 2 — and at depth 2 no further sub-run may start,
# so A→B→C runs while A→B→C→D fails with a clear error. The same counter,
# paired with the ancestor chain, blocks cycles: a workflow calling itself
# directly or through a loop of targets is rejected before the target runs.
MAX_SUBWORKFLOW_DEPTH = 2

# (depth, ancestor ids) of the chain currently executing; the default is a
# top-level chain — depth 0, no ancestors. Only sub-runs set() this, and
# always around the child execution with a paired reset in finally, so the
# state never leaks across runs or Lambda invocations.
_RUN_STATE: ContextVar = ContextVar("subworkflow_run_state", default=(0, ()))


class SubworkflowError(ValueError):
    """The sub-run cannot start (depth cap, cycle, unknown target)."""


def _render_input(value, event, steps):
    """Render template strings inside a JSON payload, like the runners do."""
    if isinstance(value, str):
        return templating.render(value, event, steps)
    if isinstance(value, dict):
        return {key: _render_input(item, event, steps) for key, item in value.items()}
    if isinstance(value, list):
        return [_render_input(item, event, steps) for item in value]
    return value


def payload_data(action, event, steps=None):
    """The event data a sub-run starts from: the ``payload`` field rendered
    against the parent event — a JSON object, or any other value wrapped as
    ``{"payload": ...}`` — or the parent event's data when omitted."""
    raw = action.get("payload")
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        return dict(event.get("data") or {})
    rendered = _render_input(raw, event, steps)
    if not isinstance(rendered, str):
        return rendered if isinstance(rendered, dict) else {"payload": rendered}
    try:
        parsed = json.loads(rendered)
    except json.JSONDecodeError:
        return {"payload": rendered}
    return parsed if isinstance(parsed, dict) else {"payload": parsed}


def find_published_workflow(workflow_id):
    """The deployed or published definition of ``workflow_id``, or None —
    the same lookup the engine's execute path iterates: the bundle with the
    published-workflows overlay merged over it, plus operator triggers."""
    for workflow in matching.all_workflows():
        if str(workflow.get("id") or "") == workflow_id:
            return workflow
    return None


def run_workflow(action, event, workflow_id=None, steps=None):
    """Run the ``workflow_id`` target's chain in-process; return its outputs.

    Raises :class:`SubworkflowError` when the target is missing, disabled, or
    would breach the depth cap or a cycle; a failing step of the target's own
    chain raises through untouched, so the calling step fails exactly like
    any other action would.
    """
    target_id = templating.render(str(action.get("workflow_id") or "").strip(), event, steps).strip()
    if not target_id:
        raise SubworkflowError("run_workflow needs a workflow_id")
    depth, ancestors = _RUN_STATE.get()
    parent = str(workflow_id or "")
    chain = [name for name in (*ancestors, parent) if name]
    if target_id in chain:
        raise SubworkflowError(
            f"run_workflow: workflow '{target_id}' cannot call itself "
            f"(cycle: {' -> '.join([*chain, target_id])})")
    if depth >= MAX_SUBWORKFLOW_DEPTH:
        raise SubworkflowError(
            f"run_workflow: sub-workflow depth cap of {MAX_SUBWORKFLOW_DEPTH} exceeded "
            f"— '{target_id}' would run at depth {depth + 1} "
            f"(chain: {' -> '.join([*chain, target_id])})")
    target = find_published_workflow(target_id)
    if target is None:
        raise SubworkflowError(
            f"run_workflow: no published or deployed workflow named '{target_id}'")
    if not target.get("enabled", True):
        raise SubworkflowError(f"run_workflow: workflow '{target_id}' is disabled")

    from .. import dryrun  # the test-run machinery this mirrors

    event_id = f"sub-{uuid.uuid4()}"
    primary = (matching.workflow_triggers(target) or [{}])[0]
    envelope = dryrun.normalize_sample_event({
        "id": event_id,
        "correlation_id": str(event.get("correlation_id") or event_id),
        "source": f"workflow:{parent or target_id}",
        "connector": str(primary.get("connector") or "custom"),
        "event": str(primary.get("event") or "received"),
        "data": payload_data(action, event, steps),
    }, target)
    # Forced matching, exactly like dryrun.execute_once: the parent asked for
    # this workflow by name, so its trigger filter cannot veto the sub-run.
    forced = {
        **target,
        "enabled": True,
        "triggers": [{"connector": envelope["connector"], "event": envelope["event"]}],
    }

    from .. import execute as engine_execute

    outputs = {}

    def after_action(_workflow_id, action_id, _event, output=None,
                     duration_ms=None, status="completed"):
        outputs[str(action_id)] = {"status": status, "output": output or {}}

    token = _RUN_STATE.set((depth + 1, tuple(chain)))
    try:
        engine_execute(envelope, after_action=after_action, workflows=[forced])
    finally:
        _RUN_STATE.reset(token)

    step_outputs = {step_id: record["output"] for step_id, record in outputs.items()}
    result = {"workflow": target_id, "steps": step_outputs}
    if any(record["status"] == "filtered" for record in outputs.values()):
        result["filtered"] = True
    output_field = str(action.get("output_field") or "").strip()
    if output_field:
        if output_field not in step_outputs:
            known = ", ".join(sorted(step_outputs)) or "none"
            raise SubworkflowError(
                f"run_workflow: workflow '{target_id}' has no step '{output_field}' "
                f"to read output_field from (steps: {known})")
        result["output"] = step_outputs[output_field]
    return result
