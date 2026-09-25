"""In-workflow logic steps: filter, condition, delay, for_each.

Logic steps live in the action chain next to connector actions and share the
same step telemetry (the before/after/error hooks), so run history shows them
like any other step. A failed filter is not an error: the step is marked
``filtered`` and the rest of the chain is skipped quietly.

Step shapes (all evaluated against the event's ``data``):

    - id: only-invoices
      type: filter
      field: subject          # dotted path; {field}/{operator}/{value} ...
      operator: contains      #   is the shorthand for one rule ...
      value: invoice          # ... or the trigger-filter style:
      # when:
      #   route: {equals: invoice}
      #   subject: {contains: invoice}

    - id: invoice-path
      type: condition
      when: {route: {equals: invoice}}
      then: [...]             # steps for the matching side
      else: [...]             # optional steps for the other side

    - id: pause
      type: delay
      seconds: 30             # bounded: 1..60 (Lambda limit)

    - id: each-attachment
      type: for_each
      list: attachments       # dotted path to a list in the event data
      item: item              # template variable bound per iteration
      actions: [...]          # steps run once per item, {item.*} rendered
"""
import json
import re
import time


# Lambda-friendly bounds: a delay never sleeps longer than a single invocation
# should, and a loop never iterates enough to blow the timeout.
MAX_DELAY_SECONDS = 60
MAX_LOOP_ITERATIONS = 100

_TOKEN = re.compile(r"\{([^{}]+)\}")


def _elapsed(started):
    return int((time.monotonic() - started) * 1000)


def _step_id(prefix, action, index):
    """The run-history id of a step; sub-steps nest under their parent."""
    own = str(action.get("id") or index)
    return f"{prefix}.{own}" if prefix else own


def _lookup(context, path):
    """Resolve a dotted path (with integer list indexes) against nested data.

    Returns (value, found); a missing path yields (None, False) so callers can
    tell "absent" from a stored null.
    """
    current = context
    for part in str(path).split("."):
        if isinstance(current, list):
            try:
                current = current[int(part)]
            except (ValueError, IndexError):
                return None, False
        elif isinstance(current, dict) and part in current:
            current = current[part]
        else:
            return None, False
    return current, True


def evaluate_rules(rules, data):
    """Evaluate trigger-filter style rules ``{field: {operator: value}}``.

    The same expression style as trigger filters (matching._matches_filter):
    every rule must pass. A bare value means equality, mirroring the engine.
    """
    from .matching import _matches_filter

    if not isinstance(rules, dict) or not rules:
        return False
    for field, rule in rules.items():
        value, _found = _lookup(data, field)
        try:
            if not _matches_filter(value, rule):
                return False
        except KeyError:
            raise ValueError(f"unknown filter operator in rule for '{field}'") from None
    return True


def predicate_rules(action):
    """A filter/condition step's predicate as ``{field: rule}``, or None.

    ``when`` is the canonical multi-rule shape; the flat ``field``/
    ``operator``/``value`` triple is the designer shorthand for one rule.
    """
    when = action.get("when")
    if isinstance(when, dict) and when:
        return when
    field = str(action.get("field") or "").strip()
    if field:
        operator = str(action.get("operator") or "equals").strip()
        return {field: {operator: action.get("value")}}
    return None


def render_value(value, context):
    """Replace ``{dotted.path}`` tokens with context values.

    Tokens that do not resolve stay verbatim, so connector runners can still
    template them against the event data (and unknown fields degrade to the
    engine's usual empty-string rendering downstream).
    """
    if isinstance(value, str):
        return _TOKEN.sub(lambda match: _rendered(context, match.group(1).strip()), value)
    if isinstance(value, dict):
        return {key: render_value(item, context) for key, item in value.items()}
    if isinstance(value, list):
        return [render_value(item, context) for item in value]
    return value


def _rendered(context, path):
    value, found = _lookup(context, path)
    if not found:
        return "{" + path + "}"
    if isinstance(value, (dict, list)):
        return json.dumps(value)
    return "" if value is None else str(value)


def run_chain(workflow_id, steps, event, run_action, *,
              before_action=None, after_action=None, on_action_error=None,
              prefix="", scope=None, step_outputs=None):
    """Run a chain of steps in order.

    ``run_action`` dispatches connector actions as ``run_action(step, event,
    workflow_id, steps=...)`` — ``steps`` is the run's accumulated step
    outputs (``{action_id: {"status": ..., "output": ...}}``), so templating
    runners can reference earlier ones; logic steps are handled here.
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
        stop = _run_step(
            workflow_id, step, index, event, run_action,
            before_action=before_action, after_action=after_action,
            on_action_error=on_action_error, prefix=prefix, scope=scope,
            step_outputs=step_outputs,
        )
        if stop:
            return stop
    return None


def _run_step(workflow_id, step, index, event, run_action, *,
              before_action, after_action, on_action_error, prefix, scope=None,
              step_outputs=None):
    if step_outputs is None:
        step_outputs = {}
    action_id = _step_id(prefix, step, index)
    if before_action and not before_action(workflow_id, action_id, event, step.get("type")):
        step_outputs[action_id] = {"status": "skipped"}
        return None
    started = time.monotonic()
    try:
        stop, output, status = _execute_step(
            workflow_id, action_id, step, event, run_action,
            before_action=before_action, after_action=after_action,
            on_action_error=on_action_error, scope=scope,
            step_outputs=step_outputs,
        )
    except Exception as exc:
        if on_action_error:
            on_action_error(workflow_id, action_id, event, exc, duration_ms=_elapsed(started))
        raise
    step_outputs[action_id] = {"status": status, "output": output or {}}
    if after_action:
        after_action(workflow_id, action_id, event, output=output or {},
                     duration_ms=_elapsed(started), status=status)
    return stop


def _execute_step(workflow_id, action_id, step, event, run_action, *,
                  before_action, after_action, on_action_error, scope=None,
                  step_outputs=None):
    """One step: returns (stop reason, output summary, run-history status)."""
    step_type = str(step.get("type"))
    if step_type == "filter":
        return _run_filter(step, _predicate_scope(event, scope))
    if step_type == "condition":
        return _run_condition(
            workflow_id, action_id, step, event, run_action,
            before_action=before_action, after_action=after_action,
            on_action_error=on_action_error, scope=scope,
            step_outputs=step_outputs,
        )
    if step_type == "delay":
        return _run_delay(step)
    if step_type == "for_each":
        return _run_for_each(
            workflow_id, action_id, step, event, run_action,
            before_action=before_action, after_action=after_action,
            on_action_error=on_action_error, scope=scope,
            step_outputs=step_outputs,
        )
    return None, run_action(step, event, workflow_id, steps=step_outputs) or {}, "completed"


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
    passed = evaluate_rules(rules, _predicate_scope(event, scope))
    branch = "then" if passed else "else"
    steps = step.get(branch) or []
    if not isinstance(steps, list):
        raise ValueError(f"condition '{action_id}' branch '{branch}' must be a list of steps")
    stop = run_chain(
        workflow_id, steps, event, run_action,
        before_action=before_action, after_action=after_action,
        on_action_error=on_action_error, prefix=f"{action_id}.{branch}",
        scope=scope, step_outputs=step_outputs,
    )
    return stop, {"condition": "passed" if passed else "failed",
                  "branch": branch, "steps": len(steps)}, "completed"


def _run_delay(step):
    seconds = step.get("seconds")
    if isinstance(seconds, bool) or not isinstance(seconds, (int, float)) or seconds < 0:
        label = step.get("id", "")
        raise ValueError(
            f"delay '{label}' needs seconds as a number between 1 and {MAX_DELAY_SECONDS}")
    slept = round(min(float(seconds), MAX_DELAY_SECONDS), 3)
    time.sleep(slept)
    return None, {"delay_seconds": seconds, "slept_seconds": slept}, "completed"


def _run_for_each(workflow_id, action_id, step, event, run_action, *,
                  before_action, after_action, on_action_error, scope=None,
                  step_outputs=None):
    label = step.get("id", "")
    body = step.get("actions")
    if not isinstance(body, list) or not body:
        raise ValueError(f"for_each '{label}' needs at least one step in actions")
    path = str(step.get("list") or "").strip().strip("{}").strip()
    if not path:
        raise ValueError(f"for_each '{label}' needs a list field")
    base = _predicate_scope(event, scope)
    items, found = _lookup(base, path)
    if not found:
        raise ValueError(f"for_each '{label}': list field '{path}' not found in the event data")
    if not isinstance(items, list):
        raise ValueError(f"for_each '{label}': field '{path}' is not a list")
    item_var = str(step.get("item") or "item").strip() or "item"
    cap = max(0, min(int(step.get("max_iterations") or MAX_LOOP_ITERATIONS),
                     MAX_LOOP_ITERATIONS))

    iterated = skipped = 0
    for position, item in enumerate(items[:cap]):
        child_scope = {**base, item_var: item, f"{item_var}_index": position}
        stop = run_chain(
            workflow_id, render_value(body, child_scope), event, run_action,
            before_action=before_action, after_action=after_action,
            on_action_error=on_action_error, prefix=f"{action_id}[{position}]",
            scope=child_scope, step_outputs=step_outputs,
        )
        iterated += 1
        # A filter inside the body skips just this iteration; the loop goes on.
        if stop == "filtered":
            skipped += 1
    return None, {"iterated": iterated, "items": len(items), "skipped": skipped,
                  "truncated": len(items) > cap}, "completed"
