"""The flow controls between steps: filters,
conditions, paths, decisions (the LLM if/switch), and delays
(the pause/resume machinery)."""
import re
import time
from .core import (MAX_DELAY_SECONDS, MAX_SUSPENDED_SECONDS, RunSuspended,
                   _TOKEN, _iso, _lookup, evaluate_rules, parse_moment,
                   predicate_rules, render_value)


def _predicate_scope(event, scope):
    """The data a predicate reads: the loop's scope when inside one."""
    if scope is not None:
        return scope
    return event.get("data") or {}


def _collect_branch_summary(branch_prefix, step_outputs):
    """Summarize the outputs recorded under ``<branch_prefix>.*``."""
    if not branch_prefix or not isinstance(step_outputs, dict):
        return {"last_step_id": None, "result": {}, "outputs": {}}
    prefix = f"{branch_prefix}."
    outputs = {}
    last_step_id = None
    for key, entry in step_outputs.items():
        if not isinstance(key, str) or not key.startswith(prefix):
            continue
        local_id = key[len(prefix):]
        if not local_id:
            continue
        out = entry.get("output") if isinstance(entry, dict) else None
        outputs[local_id] = dict(out) if isinstance(out, dict) else (out if out is not None else {})
        last_step_id = key
    if last_step_id is not None:
        last_entry = step_outputs.get(last_step_id)
        raw_result = last_entry.get("output") if isinstance(last_entry, dict) else {}
        result = dict(raw_result) if isinstance(raw_result, dict) else (raw_result if raw_result is not None else {})
    else:
        result = {}
    return {
        "last_step_id": last_step_id,
        "result": result,
        "outputs": outputs,
    }


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
    summary = _collect_branch_summary(f"{action_id}.{branch}", step_outputs)
    return stop, {"condition": "passed" if passed else "failed",
                  "branch": branch, "steps": len(steps), **summary}, "completed"


def _run_paths(workflow_id, action_id, step, event, run_action, *,
               before_action, after_action, on_action_error, scope=None,
               step_outputs=None):
    """Zapier-Paths branching: run the first branch whose predicate matches.

    Branches are evaluated in order against the same scope filters read, and
    the first match wins — later predicates are not evaluated. With no match
    the optional ``default`` (or ``else``) steps run. A filter inside a branch
    stops the chain quietly, exactly like condition branches.
    """
    branches = step.get("paths")
    if not isinstance(branches, list) or not branches:
        raise ValueError(f"paths '{step.get('id', '')}' needs a non-empty list of paths")
    default_steps = step.get("default") if step.get("default") is not None else step.get("else")
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
        summary = _collect_branch_summary(f"{action_id}.{label}", step_outputs)
        return stop, {"matched": label, "branch": label, "ran": len(steps), **summary}, "completed"
    if default_steps:
        stop = run_chain(
            workflow_id, default_steps, event, run_action,
            before_action=before_action, after_action=after_action,
            on_action_error=on_action_error, prefix=f"{action_id}.default",
            scope=scope, step_outputs=step_outputs,
        )
        summary = _collect_branch_summary(f"{action_id}.default", step_outputs)
        return stop, {"matched": None, "branch": "default", "ran": len(default_steps), **summary}, "completed"
    return None, {"matched": None, "branch": None, "ran": 0,
                  "last_step_id": None, "result": {}, "outputs": {}}, "completed"


# --- decision: the LLM smart if / switch -----------------------------------

DECISION_MODES = ("if", "switch")


def _decision_mode(step):
    """The decision step's mode: ``if`` or ``switch``.

    Explicit when ``mode:`` says so; otherwise inferred — a step carrying
    ``choices``/``cases`` is a switch, everything else an if. The same rule
    the save-time validator and the dry-run apply, so all three surfaces
    agree on what a step means.
    """
    mode = str(step.get("mode") or "").strip().lower()
    if mode:
        return mode
    if "choices" in step or "cases" in step:
        return "switch"
    return "if"


def _decision_choices(step, action_id):
    """The switch step's allowed choices as a clean, order-kept, unique
    list of non-empty strings — the enum the LLM's structured reply is
    constrained to, and the keys the ``cases`` branches match."""
    choices = step.get("choices")
    if not isinstance(choices, list) or not choices:
        raise ValueError(f"decision '{action_id}' needs a non-empty list of choices")
    clean = []
    for choice in choices:
        text = str(choice or "").strip()
        if not text:
            raise ValueError(f"decision '{action_id}': choices must be non-empty strings")
        if text not in clean:
            clean.append(text)
    return clean


def _decision_plan(step, action_id):
    """Shape-check a decision step without touching the LLM: the mode plus,
    for a switch, its choices. Shared by the engine, the dry-run and the
    save-time validator, so a step that passes here runs and validates."""
    mode = _decision_mode(step)
    if mode not in DECISION_MODES:
        raise ValueError(
            f"decision '{action_id}': mode must be one of {', '.join(DECISION_MODES)}")
    if not str(step.get("prompt") or "").strip():
        raise ValueError(f"decision '{action_id}' needs a prompt")
    return mode, (_decision_choices(step, action_id) if mode == "switch" else None)


def _decision_fallback_steps(step):
    """Fallback step list for a switch decision (default_actions / default / else)."""
    for fallback_key in ("default_actions", "default", "else"):
        if step.get(fallback_key) is not None:
            return step.get(fallback_key)
    return None


def _decision_branch(workflow_id, action_id, step, event, run_action, *,
                     before_action, after_action, on_action_error, scope,
                     step_outputs, mode, choice, model, usage, note=None,
                     extra=None):
    """Run the branch a decision picked: ``then``/``else`` for an if, the
    matching case's ``actions`` (or ``default_actions`` / ``default`` / ``else``)
    for a switch. The branch id nests under the step like a condition's or a
    path's, and the output carries the choice and the branch so later steps
    can template ``{steps.<id>.choice}`` and react without re-asking the model."""
    if mode == "if":
        branch = "then" if choice else "else"
        steps = step.get(branch) or []
    elif choice is None:
        branch = "default"
        steps = _decision_fallback_steps(step) or []
    else:
        branch = choice
        steps = []
        for case in step.get("cases") or []:
            if isinstance(case, dict) and str(case.get("choice") or "").strip() == choice:
                steps = case.get("actions") or []
                break
    if not isinstance(steps, list):
        raise ValueError(f"decision '{action_id}' branch '{branch}' must be a list of steps")
    from .chains import run_chain  # lazy: chains (transitively) imports this module

    stop = run_chain(
        workflow_id, steps, event, run_action,
        before_action=before_action, after_action=after_action,
        on_action_error=on_action_error, prefix=f"{action_id}.{branch}",
        scope=scope, step_outputs=step_outputs,
    )
    summary = _collect_branch_summary(f"{action_id}.{branch}", step_outputs)
    output = {"mode": mode,
              "choice": choice if choice is not None else "",
              "branch": branch, "steps": len(steps), "model": model,
              "usage": usage, **summary}
    output.update(extra or {})
    if note:
        output["note"] = note
    return stop, output, "completed"


def _run_decision(workflow_id, action_id, step, event, run_action, *,
                  before_action, after_action, on_action_error, scope=None,
                  step_outputs=None, transport=None):
    """The LLM decision step: a smart ``if`` statement or a ``switch``.

    One typed call to OpenRouter's decisions endpoint (the jev route,
    ``~typesafe/jev-latest`` by default — step ``model:`` overrides): the
    rendered prompt is the question's ``state`` and the model answers a
    single choice question over the criteria, so the reply is a picked
    option with probabilities and confidence — no free text to parse.
    ``mode: if`` offers yes/no and runs ``then``/``else``; ``mode: switch``
    offers ``choices`` (a case may carry a ``description:`` that becomes
    that option's criterion) and runs the matching case's ``actions``.
    A response with no usable choice — or one outside ``choices`` — runs
    ``default_actions`` (or ``default`` / ``else``) when the step has one and
    fails the step otherwise, so on_fail/on_error policies apply. ``system``,
    when set, is the question's instructions.
    """
    from ...connectors import ai
    from ..actions.templating import render

    mode, choices = _decision_plan(step, action_id)
    credential = ai.openrouter_credential()
    if not credential or not str(credential.get("api_key") or "").strip():
        raise ValueError(
            f"decision '{action_id}' is not configured (store the OpenRouter "
            f"key as the '{ai.OPENROUTER_CREDENTIAL_ID}' credential — `dapier "
            f"credentials set {ai.OPENROUTER_CREDENTIAL_ID}` or the console "
            "Credentials tab)")
    prompt = render(str(step["prompt"]), event, step_outputs).strip()
    if mode == "switch":
        described = {}
        for case in step.get("cases") or []:
            if isinstance(case, dict):
                choice = str(case.get("choice") or "").strip()
                description = str(case.get("description") or "").strip()
                if choice and description:
                    described[choice] = description
        criteria = {choice: described.get(choice, choice) for choice in choices}
        instructions = (str(step.get("system") or "").strip()
                        or "Pick exactly the one option that matches the state.")
    else:
        criteria = {"yes": "the answer to the state's question is yes / true",
                    "no": "the answer to the state's question is no / false"}
        instructions = (str(step.get("system") or "").strip()
                        or "Answer the state's yes/no question by choosing "
                           "exactly one option.")
    result = ai.run_decisions(
        state=prompt, instructions=instructions, criteria=criteria,
        model=step.get("model"),
        timeout_seconds=step.get("timeout_seconds"), transport=transport,
        credential=credential, workflow_id=workflow_id)
    model, usage = result.get("model"), (result.get("usage") if isinstance(result.get("usage"), dict) else {})
    extra = {"confidence": result.get("confidence"),
             "probabilities": result.get("probabilities")}

    def _unusable(note):
        default_steps = _decision_fallback_steps(step)
        if mode == "switch" and isinstance(default_steps, list):
            return _decision_branch(
                workflow_id, action_id, step, event, run_action,
                before_action=before_action, after_action=after_action,
                on_action_error=on_action_error, scope=scope,
                step_outputs=step_outputs, mode=mode, choice=None,
                model=model, usage=usage, note=note)
        raise ValueError(f"decision '{action_id}': {note}")

    if not result.get("ok"):
        return _unusable(str(result.get("error") or "the model returned no choice"))
    choice = result["choice"]
    if mode == "switch":
        if choice not in choices:
            return _unusable(f"the model answered '{choice}', which is not one of "
                             f"{', '.join(choices)}")
        picked = choice
    else:
        if choice not in ("yes", "no"):
            return _unusable(f"the model answered '{choice}', which is not yes or no")
        picked = choice == "yes"
    return _decision_branch(
        workflow_id, action_id, step, event, run_action,
        before_action=before_action, after_action=after_action,
        on_action_error=on_action_error, scope=scope,
        step_outputs=step_outputs, mode=mode, choice=picked,
        model=model, usage=usage, extra=extra)


def _resolve_union_candidate(candidate, context):
    """Evaluate one union field candidate against ``context``.

    Returns ``(value, resolved_ok)`` where ``resolved_ok`` is True when the
    candidate resolved to a non-empty value without unexpanded ``{...}`` tokens.
    """
    if candidate is None:
        return None, False
    if isinstance(candidate, str):
        stripped = candidate.strip()
        if not stripped:
            return "", False
        if _TOKEN.search(candidate):
            single = re.fullmatch(r"\{([^{}]+)\}", stripped)
            if single:
                val, found = _lookup(context, single.group(1).strip())
                if not found or val is None or val == "":
                    return "", False
                return val, True
            rendered = render_value(candidate, context)
            if isinstance(rendered, str) and (_TOKEN.search(rendered) or rendered == ""):
                return "", False
            return rendered, True
        val, found = _lookup(context, stripped)
        if found and val is not None and val != "":
            return val, True
        if stripped.startswith(("steps.", "trigger.")):
            return "", False
        return candidate, True
    return candidate, True


def _run_union(workflow_id, action_id, step, event, run_connector=None, *,
               before_action=None, after_action=None, on_action_error=None,
               prefix="", scope=None, step_outputs=None):
    """Explicit branch convergence and output coalescing (``type: union``)."""
    del workflow_id, action_id, run_connector, before_action, after_action, on_action_error, prefix
    source_id = str(step.get("from") or "").strip()
    if not source_id and isinstance(step_outputs, dict):
        for candidate_id in reversed(list(step_outputs.keys())):
            entry = step_outputs.get(candidate_id)
            out = entry.get("output") if isinstance(entry, dict) else None
            if isinstance(out, dict) and ("branch" in out or "matched" in out) and "from" not in out:
                source_id = str(candidate_id)
                break
    source_entry = (step_outputs or {}).get(source_id) if source_id and isinstance(step_outputs, dict) else None
    source_out = (source_entry.get("output")
                  if isinstance(source_entry, dict) and isinstance(source_entry.get("output"), dict)
                  else {})
    branch = source_out.get("branch") if "branch" in source_out else source_out.get("matched")
    last_step_id = source_out.get("last_step_id")
    result = source_out.get("result") if "result" in source_out else {}
    outputs = source_out.get("outputs") if isinstance(source_out.get("outputs"), dict) else {}
    if source_id and not outputs and isinstance(step_outputs, dict):
        inferred_branch = branch
        if not inferred_branch:
            src_prefix = f"{source_id}."
            for key in step_outputs:
                if isinstance(key, str) and key.startswith(src_prefix):
                    rest = key[len(src_prefix):]
                    inferred_branch = rest.split(".", 1)[0] if "." in rest else rest
                    break
        if inferred_branch:
            branch = branch or inferred_branch
            summary = _collect_branch_summary(f"{source_id}.{inferred_branch}", step_outputs)
            last_step_id = last_step_id or summary["last_step_id"]
            if not result:
                result = summary["result"]
            outputs = summary["outputs"]

    context = {
        **_predicate_scope(event, scope),
        "trigger": event or {},
        "steps": step_outputs or {},
    }
    fields_spec = step.get("fields")
    values = {}
    if isinstance(fields_spec, dict):
        for field_name, spec in fields_spec.items():
            candidates = spec if isinstance(spec, list) else [spec]
            picked = ""
            for cand in candidates:
                val, ok = _resolve_union_candidate(cand, context)
                if ok:
                    picked = val
                    break
            values[str(field_name)] = picked

    out = {
        "from": source_id or None,
        "branch": branch,
        "last_step_id": last_step_id,
        "result": dict(result) if isinstance(result, dict) else result,
        "outputs": dict(outputs) if isinstance(outputs, dict) else {},
        "values": values,
    }
    for key, val in values.items():
        if key not in out:
            out[key] = val
    return None, out, "completed"


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


