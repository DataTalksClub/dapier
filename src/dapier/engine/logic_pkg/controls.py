"""The flow controls between steps: filters,
conditions, paths, and delays (the pause/resume machinery)."""
import time
from .core import (MAX_DELAY_SECONDS, MAX_SUSPENDED_SECONDS, RunSuspended, _iso,
                   evaluate_rules, parse_moment, predicate_rules, render_value)


def _predicate_scope(event, scope):
    """The data a predicate reads: the loop's scope when inside one."""
    if scope is not None:
        return scope
    return event.get("data") or {}


def _run_filter(step, data):
    rules = predicate_rules(step)
    if rules is None:
        raise ValueError(f"filter '{step.get('id', '')}' needs a when mapping or a field")
    if evaluate_rules(rules, data):
        return None, {"filter": "passed"}, "completed"
    return "filtered", {"filter": "stopped", "when": rules}, "filtered"


def _run_condition(workflow_id, action_id, step, event, run_action, *,
                   before_action, after_action, on_action_error, scope=None,
                   step_outputs=None):
    rules = predicate_rules(step)
    if rules is None:
        raise ValueError(f"condition '{step.get('id', '')}' needs a when mapping or a field")
    passed = evaluate_rules(
        rules, {**_predicate_scope(event, scope), "steps": step_outputs or {}})
    branch = "then" if passed else "else"
    steps = step.get(branch) or []
    if not isinstance(steps, list):
        raise ValueError(f"condition '{action_id}' branch '{branch}' must be a list of steps")
    from .chains import run_chain  # lazy: chains (transitively) imports this module

    stop = run_chain(
        workflow_id, steps, event, run_action,
        before_action=before_action, after_action=after_action,
        on_action_error=on_action_error, prefix=f"{action_id}.{branch}",
        scope=scope, step_outputs=step_outputs,
    )
    return stop, {"condition": "passed" if passed else "failed",
                  "branch": branch, "steps": len(steps)}, "completed"


def _run_paths(workflow_id, action_id, step, event, run_action, *,
               before_action, after_action, on_action_error, scope=None,
               step_outputs=None):
    """Zapier-Paths branching: run the first branch whose predicate matches.

    Branches are evaluated in order against the same scope filters read, and
    the first match wins — later predicates are not evaluated. With no match
    the optional ``default`` steps run. A filter inside a branch stops the
    chain quietly, exactly like condition branches.
    """
    branches = step.get("paths")
    if not isinstance(branches, list) or not branches:
        raise ValueError(f"paths '{step.get('id', '')}' needs a non-empty list of paths")
    default_steps = step.get("default")
    if default_steps is not None and not isinstance(default_steps, list):
        raise ValueError(f"paths '{action_id}' default must be a list of steps")
    data = {**_predicate_scope(event, scope), "steps": step_outputs or {}}
    from .chains import run_chain  # lazy: chains (transitively) imports this module

    for branch in branches:
        if not isinstance(branch, dict):
            raise ValueError(f"paths '{action_id}': every path must be a mapping")
        rules = predicate_rules(branch)
        if rules is None:
            raise ValueError(
                f"paths '{action_id}': path '{branch.get('label', '')}' "
                "needs a when mapping or a field")
        if not evaluate_rules(rules, data):
            continue
        label = str(branch.get("label") or "").strip() or "path"
        steps = branch.get("actions") or []
        if not isinstance(steps, list):
            raise ValueError(
                f"paths '{action_id}' path '{label}' actions must be a list of steps")
        stop = run_chain(
            workflow_id, steps, event, run_action,
            before_action=before_action, after_action=after_action,
            on_action_error=on_action_error, prefix=f"{action_id}.{label}",
            scope=scope, step_outputs=step_outputs,
        )
        return stop, {"matched": label, "ran": len(steps)}, "completed"
    if default_steps:
        stop = run_chain(
            workflow_id, default_steps, event, run_action,
            before_action=before_action, after_action=after_action,
            on_action_error=on_action_error, prefix=f"{action_id}.default",
            scope=scope, step_outputs=step_outputs,
        )
        return stop, {"matched": None, "ran": len(default_steps)}, "completed"
    return None, {"matched": None, "ran": 0}, "completed"


def _run_delay(step, context):
    """Pause the chain. Up to MAX_DELAY_SECONDS the step sleeps inline; a
    longer pause raises RunSuspended so the run parks and resumes later
    (Zapier's Delay For / Delay Until, unbounded by one invocation)."""
    label = step.get("id", "")
    total, until = _delay_request(step, context, label)
    if until is not None:
        total = max(0.0, until - time.time())
    if total <= MAX_DELAY_SECONDS:
        slept = round(total, 3)
        time.sleep(slept)
        return None, {"delay_seconds": round(total, 3), "slept_seconds": slept}, "completed"
    resume_at = time.time() + total
    raise RunSuspended(resume_at, {
        "delay_seconds": round(total, 3), "resume_at": _iso(resume_at),
        "suspended": True,
    })


DURATION_KEYS = ("days", "hours", "minutes", "seconds")

# Each duration field names its unit; the requested wait is the converted
# sum, so ``minutes: 5`` is five minutes, not five seconds.
DURATION_UNITS = {"days": 86400, "hours": 3600, "minutes": 60, "seconds": 1}


def _delay_request(step, context, label):
    """The requested pause as ``(total_seconds, until_epoch)``.

    Durations combine (``days``/``hours``/``minutes``/``seconds``, numbers,
    each template-renderable against the step context); ``until`` is an ISO
    datetime, also template-renderable, and excludes the duration fields.
    """
    until = step.get("until")
    durations = {}
    for key in DURATION_KEYS:
        if step.get(key) is None:
            continue
        durations[key] = _delay_number(step.get(key), key, label, context)
    if until is not None:
        if any(value > 0 for value in durations.values()):
            raise ValueError(f"delay '{label}': set until or a duration, not both")
        moment = parse_moment(render_value(until, context))
        if moment is None:
            raise ValueError(
                f"delay '{label}': until must be an ISO 8601 datetime "
                "(or a template rendering one)")
        if moment - time.time() > MAX_SUSPENDED_SECONDS:
            # Same ceiling as the duration fields: a suspension may not
            # outlive the executions table's 90-day TTL.
            raise ValueError(
                f"delay '{label}': a delay may not exceed {MAX_SUSPENDED_SECONDS // 86400} days")
        return None, moment
    if not durations:
        raise ValueError(
            f"delay '{label}' needs seconds (or minutes, hours, days, or until) "
            "as a positive number")
    total = round(sum(
        durations[key] * DURATION_UNITS[key] for key in durations), 6)
    if total <= 0:
        raise ValueError(
            f"delay '{label}' needs seconds (or minutes, hours, days, or until) "
            "as a positive number")
    if total > MAX_SUSPENDED_SECONDS:
        raise ValueError(
            f"delay '{label}': a delay may not exceed {MAX_SUSPENDED_SECONDS // 86400} days")
    return total, None


def _delay_number(value, key, label, context):
    rendered = render_value(value, context)
    if isinstance(rendered, str):
        try:
            rendered = float(rendered.strip())
        except ValueError:
            raise ValueError(
                f"delay '{label}': {key} must be a number (or a template rendering one)"
            ) from None
    if isinstance(rendered, bool) or not isinstance(rendered, (int, float)) or rendered < 0:
        raise ValueError(
            f"delay '{label}': {key} must be a number (or a template rendering one)")
    return float(rendered)


