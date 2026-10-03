"""One step's execution: the error modes, the
autoretry planning and backoff, and the dispatch into the
action runner."""
import random
import time
from .controls import (_predicate_scope, _run_condition, _run_delay, _run_filter,
                       _run_paths)
from .core import (AUTORETRY_DEFAULTS, CompletedStep, ERROR_MODES, ON_FAIL_MODES,
                   QuotaExceeded, RunSuspended, _elapsed, _step_id)
from .loops import _run_digest, _run_for_each


def _run_step(workflow_id, step, index, event, run_action, *,
              before_action, after_action, on_action_error, prefix, scope=None,
              step_outputs=None):
    if step_outputs is None:
        step_outputs = {}
    action_id = _step_id(prefix, step, index)
    quota_gate = None
    if before_action:
        try:
            gate = before_action(workflow_id, action_id, event, step.get("type"))
        except QuotaExceeded as exc:
            # A spent task budget refuses the step before the connector
            # call; the re-raise inside the try below hands it to the
            # ordinary error machinery so the step record, the on_fail /
            # on_error policy and the retry tagging all apply exactly as
            # for a connector failure. Anything else the gate raises
            # (LeaseBusy) keeps its own semantics.
            quota_gate = exc
        else:
            if not gate:
                if isinstance(gate, CompletedStep):
                    step_outputs[action_id] = {"status": gate.status, "output": gate.output}
                    if gate.status == "filtered":
                        return "filtered"
                else:
                    step_outputs[action_id] = {"status": "skipped"}
                return None
    # The autoretry plan is parsed and validated before the step runs: a bad
    # config is an authoring error, loud even when on_fail would absorb the
    # step's failures (mirroring the lazy on_fail/on_error checks).
    autoretry = _autoretry_plan(step, action_id)
    started = time.monotonic()
    try:
        if quota_gate is not None:
            raise quota_gate
        stop, output, status = _execute_step(
            workflow_id, action_id, step, event, run_action,
            before_action=before_action, after_action=after_action,
            on_action_error=on_action_error, scope=scope,
            step_outputs=step_outputs, autoretry=autoretry,
        )
    except RunSuspended as susp:
        # Suspension is not a failure and no error policy applies: the step
        # closes out ``delayed`` (run history shows the pause) and the
        # unwind continues so every enclosing chain appends its remainder.
        # Every frame the unwind passes through records itself, so the
        # resume can close the pause out ``completed`` again; the innermost
        # frame — the delay step itself — owns ``delay_action_id``.
        susp.paused_ids.append(action_id)
        if susp.delay_action_id is None:
            susp.delay_action_id = action_id
        susp.step_outputs = step_outputs
        step_outputs[action_id] = {"status": "delayed", "output": dict(susp.output)}
        if after_action:
            after_action(workflow_id, action_id, event, output=dict(susp.output),
                         duration_ms=_elapsed(started), status="delayed")
        raise
    except Exception as exc:
        # ``on_fail`` is the step's own failure policy (Zapier's error
        # setting); ``on_error`` is the richer handled-failure policy below.
        # The two are mutually exclusive — checked when on_fail is set.
        on_fail = str(step.get("on_fail") or "").strip().lower()
        if on_fail and on_fail not in ON_FAIL_MODES:
            raise ValueError(
                f"step '{action_id}': on_fail must be one of {', '.join(ON_FAIL_MODES)}"
            ) from exc
        if on_fail and str(step.get("on_error") or "").strip():
            raise ValueError(
                f"step '{action_id}': set on_fail or on_error, not both") from exc
        if on_fail == "continue":
            # Absorb the failure: the error hook still fires (the attempt
            # did fail — the worker releases the step record and the error
            # message lands on it), then the step closes out ``skipped``
            # through the after hook and the chain runs on.
            if on_action_error:
                on_action_error(workflow_id, action_id, event, exc,
                                duration_ms=_elapsed(started))
            message = str(exc) or exc.__class__.__name__
            step_outputs[action_id] = {"status": "skipped", "error": message, "output": {}}
            if after_action:
                after_action(workflow_id, action_id, event, output={},
                             duration_ms=_elapsed(started), status="skipped")
            return None
        mode = str(step.get("on_error") or "halt").strip().lower() or "halt"
        if mode not in ERROR_MODES:
            raise ValueError(
                f"step '{action_id}': on_error must be one of {', '.join(ERROR_MODES)}") from exc
        # Tag the failing workflow for the worker's failure
        # notifications (see engine.notify), plus the innermost failing step
        # so a retry policy can tell an action failure (retryable) from a
        # logic-step problem (a config error fails fast). The innermost
        # chain tags first; outer levels keep it.
        exc.dapier_workflow = workflow_id
        exc.dapier_step_id = getattr(exc, "dapier_step_id", None) or action_id
        exc.dapier_step_type = getattr(exc, "dapier_step_type", None) or str(step.get("type") or "")
        if on_action_error:
            on_action_error(workflow_id, action_id, event, exc, duration_ms=_elapsed(started))
        if mode == "halt":
            raise
        return _handle_error(
            workflow_id, step, action_id, exc, event, run_action,
            before_action=before_action, after_action=after_action,
            on_action_error=on_action_error, scope=scope,
            step_outputs=step_outputs, duration_ms=_elapsed(started),
        )
    step_outputs[action_id] = {"status": status, "output": output or {}}
    if after_action:
        after_action(workflow_id, action_id, event, output=output or {},
                     duration_ms=_elapsed(started), status=status)
    return stop


def _handle_error(workflow_id, step, action_id, exc, event, run_action, *,
                  before_action, after_action, on_action_error, scope=None,
                  step_outputs=None, duration_ms=None):
    """A step failed with ``on_error: continue|run``: record the failure and
    move on instead of aborting the run.

    The failed step lands in the ``steps`` context as
    ``{"status": "failed", "error": ..., "output": {}}`` — the error message
    is what ``{steps.<id>.error}`` renders — and ``after_action`` reports the
    ``failed`` status so run history shows the step like any other. With
    ``run``, ``error_actions`` executes as a sub-chain (ids prefixed
    ``<step>.error``) after the failure is recorded, so the branch can
    template the error. Either way the chain proceeds: a filter inside the
    error branch stops the run quietly, like any filtered branch.
    """
    mode = str(step.get("on_error") or "").strip().lower()
    error_actions = step.get("error_actions")
    if mode == "run" and (not isinstance(error_actions, list) or not error_actions):
        raise ValueError(
            f"step '{action_id}': on_error run needs a non-empty error_actions list") from exc
    message = str(exc) or exc.__class__.__name__
    step_outputs[action_id] = {"status": "failed", "error": message, "output": {}}
    if after_action:
        after_action(workflow_id, action_id, event, output={},
                     duration_ms=duration_ms, status="failed")
    if mode != "run":
        return None
    from .chains import run_chain  # lazy: chains imports this module at its top

    return run_chain(
        workflow_id, error_actions, event, run_action,
        before_action=before_action, after_action=after_action,
        on_action_error=on_action_error, prefix=f"{action_id}.error",
        scope=scope, step_outputs=step_outputs,
    )


def _execute_step(workflow_id, action_id, step, event, run_action, *,
                  before_action, after_action, on_action_error, scope=None,
                  step_outputs=None, autoretry=None):
    """One step: returns (stop reason, output summary, run-history status)."""
    step_type = str(step.get("type"))
    if step_type == "filter":
        # Mid-chain routing sees earlier outputs too: the rules read the
        # trigger data (or the loop's scope) plus ``steps``, like the
        # delay/digest contexts below.
        return _run_filter(
            step, {**_predicate_scope(event, scope), "steps": step_outputs or {}})
    if step_type == "condition":
        return _run_condition(
            workflow_id, action_id, step, event, run_action,
            before_action=before_action, after_action=after_action,
            on_action_error=on_action_error, scope=scope,
            step_outputs=step_outputs,
        )
    if step_type == "delay":
        context = {**_predicate_scope(event, scope), "steps": step_outputs}
        return _run_delay(step, context)
    if step_type == "paths":
        return _run_paths(
            workflow_id, action_id, step, event, run_action,
            before_action=before_action, after_action=after_action,
            on_action_error=on_action_error, scope=scope,
            step_outputs=step_outputs,
        )
    if step_type == "for_each":
        return _run_for_each(
            workflow_id, action_id, step, event, run_action,
            before_action=before_action, after_action=after_action,
            on_action_error=on_action_error, scope=scope,
            step_outputs=step_outputs,
        )
    if step_type == "digest":
        context = {**_predicate_scope(event, scope), "steps": step_outputs}
        return _run_digest(workflow_id, step, event, context)
    return None, _dispatch_action(
        step, event, workflow_id, run_action, step_outputs, autoretry), "completed"


def _dispatch_action(step, event, workflow_id, run_action, step_outputs, autoretry):
    """One connector action through the caller's dispatch.

    Without an autoretry plan this is today's single call. With one
    (Zapier's autoretry) a failure is retried up to ``attempts`` times with
    exponential backoff; once the retries are exhausted the exception
    propagates untouched — the step's ``on_fail``/``on_error`` policy (and
    past it the workflow-level ``retry`` redrive) sees the failure exactly
    as a single attempt would have. A step that needed its retries records
    ``attempts`` (the tries made) in its output, so run history shows a
    step that succeeded on try 3.
    """
    if not autoretry:
        return run_action(step, event, workflow_id, steps=step_outputs) or {}
    attempts = int(autoretry["attempts"])
    for tries in range(1, attempts + 2):
        try:
            output = run_action(step, event, workflow_id, steps=step_outputs) or {}
        except Exception as exc:
            if tries > attempts:
                raise
            time.sleep(_autoretry_backoff(autoretry, tries, error=exc))
        else:
            output = dict(output)
            output["attempts"] = tries
            return output


def _autoretry_plan(step, action_id):
    """The step's validated autoretry config as a plan dict with defaults
    filled in, or None when the step does not opt in.

    The shape and bounds are validated by ``registry.validate_autoretry_key``
    (the save-time validator shares the checks, so both surfaces reject a
    bad config with the same messages); the engine imports it lazily —
    engine.logic stays import-light. A logic step carrying the key is
    rejected here too.
    """
    config = step.get("autoretry")
    if config is None or (isinstance(config, str) and not config.strip()):
        return None
    from ...connectors import registry

    registry.validate_autoretry_key(step, f"step '{action_id}'", ValueError)
    plan = {**AUTORETRY_DEFAULTS, **config}
    return {"attempts": int(plan["attempts"]),
            "initial_seconds": float(plan["initial_seconds"]),
            "max_seconds": float(plan["max_seconds"])}


def _autoretry_backoff(plan, retry_number, error=None):
    """Seconds to wait before retry ``retry_number`` (1-based): the initial
    delay doubling each retry, capped at ``max_seconds``, plus up to a
    quarter of the delay as jitter so many failing steps do not retry in
    lockstep. A failure carrying an HTTP 429/503 status and the server's
    ``Retry-After`` hint (the webhook/http_request ``HttpError``) waits the
    hinted delay for that step's backoff instead of the doubling one — the
    provider's own pacing, still capped at ``max_seconds``. Bounds-checked
    at save time, the inline sleeps stay small."""
    hinted = _retry_after_hint(error, plan)
    delay = hinted if hinted is not None else min(
        float(plan["max_seconds"]),
        float(plan["initial_seconds"]) * (2 ** (retry_number - 1)))
    return delay + random.uniform(0, delay / 4)


def _retry_after_hint(error, plan):
    """The Retry-After seconds a rate-limited (429/503) failure asks for,
    capped at the plan's max sleep; None when there is no usable hint.

    Only the delay-in-seconds form is honored (the webhook/http actions
    parse nothing else); a hint at or below zero is no hint at all.
    """
    if error is None or getattr(error, "status", None) not in (429, 503):
        return None
    try:
        seconds = float(getattr(error, "retry_after", None))
    except (TypeError, ValueError):
        return None
    if seconds <= 0:
        return None
    return min(float(plan["max_seconds"]), seconds)


