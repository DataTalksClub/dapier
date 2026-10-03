"""run_chain and resume_chain: the ordered walk
over a workflow's steps and the suspension segments that park
between them."""
from .core import RunSuspended
from .execution import _run_step
from .loops import _run_for_each


def run_chain(workflow_id, steps, event, run_action, *,
              before_action=None, after_action=None, on_action_error=None,
              prefix="", scope=None, step_outputs=None):
    """Run a chain of steps in order.

    ``run_action`` dispatches connector actions as ``run_action(step, event,
    workflow_id, steps=...)`` — ``steps`` is the run's accumulated step
    outputs (``{action_id: {"status": ..., "output": ...}}``; a step that
    failed under ``on_error: continue|run`` also carries ``"error"``), so
    templating runners can reference earlier ones; logic steps are handled
    here.
    Sub-chains (condition branches, loop bodies) recurse into run_chain, so
    every nested step gets the same telemetry hooks and ids. ``scope`` is the
    data predicates evaluate against (loop bodies bind ``{item}`` into it);
    it defaults to the event data. Returns why the chain stopped early
    (``"filtered"``) or None when it ran to the end.
    """
    if step_outputs is None:
        step_outputs = {}
    for index, step in enumerate(steps):
        if not isinstance(step, dict) or not str(step.get("type") or "").strip():
            raise ValueError(f"step {index} needs a type")
        try:
            stop = _run_step(
                workflow_id, step, index, event, run_action,
                before_action=before_action, after_action=after_action,
                on_action_error=on_action_error, prefix=prefix, scope=scope,
                step_outputs=step_outputs,
            )
        except RunSuspended as susp:
            # This chain's remainder resumes after whatever paused inside
            # the step; segments accumulate innermost-first while the
            # suspension unwinds.
            remaining = steps[index + 1:]
            if remaining:
                susp.segments.append(
                    {"steps": remaining, "prefix": prefix, "scope": scope})
            raise
        if stop:
            return stop
    return None


def resume_chain(workflow_id, segments, event, run_action, *,
                 before_action=None, after_action=None, on_action_error=None,
                 step_outputs=None):
    """Continue a suspended run over its ``RunSuspended.segments``.

    A segment is one chain's un-run remainder — ``{"steps": [...], "prefix":
    ..., "scope": ...}`` — or a paused loop — ``{"loop": {"action_id",
    "step", "from", "scope", "item_var", "cap"}}`` — ordered as the run
    would have gone: the chain that paused first, then each enclosing
    chain's tail. The accumulated ``step_outputs`` carry over, so resumed
    steps still see the outputs of everything before the pause. Segments
    run in order and the first stop (``filtered``) skips the ones behind
    it, exactly like the original chain walk. A delay inside the remainder
    suspends again (``RunSuspended`` unwinds with the segments that are
    still left), so any number of pauses chain.
    """
    if step_outputs is None:
        step_outputs = {}
    for position, segment in enumerate(segments):
        try:
            loop = segment.get("loop") if isinstance(segment, dict) else None
            if loop:
                _run_for_each(
                    workflow_id, str(loop.get("action_id") or ""), loop.get("step") or {},
                    event, run_action,
                    before_action=before_action, after_action=after_action,
                    on_action_error=on_action_error, scope=loop.get("scope"),
                    step_outputs=step_outputs, start=int(loop.get("from") or 0),
                )
                continue
            stop = run_chain(
                workflow_id, (segment or {}).get("steps") or [], event, run_action,
                before_action=before_action, after_action=after_action,
                on_action_error=on_action_error,
                prefix=str((segment or {}).get("prefix") or ""),
                scope=(segment or {}).get("scope"),
                step_outputs=step_outputs,
            )
            if stop:
                return stop
        except RunSuspended as susp:
            # The replay paused again: the new suspension carries its own
            # unwound remainder (innermost first) plus every segment still
            # waiting behind this one, or the enclosing chains' tails would
            # silently never run.
            susp.segments.extend(segments[position + 1:])
            raise
    return None


