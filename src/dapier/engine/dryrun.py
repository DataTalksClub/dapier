"""Test-before-publish: run one workflow against a sample event.

The safety net behind the designer's save (which publishes instantly): a
dry-run resolves the workflow's action chain — including its ``flow``
binding — and walks it rendering every step's inputs against the sample
event, with the real runners never invoked, so nothing is sent, posted, or
written. Validation problems (unknown action type, undefined flow, broken
template) surface per step exactly as the engine would fail them at run
time.

``execute=True`` runs the same chain through the real engine execution path
(the ``execute`` dispatcher with the real runners) restricted to this one
workflow, so side effects are intended and limited to the workflow under
test. Nothing is recorded to the executions table — run history stays
reserved for real trigger traffic.

Templating mirrors the action runners: ``{token}`` fields format against the
sample event's data (missing tokens render empty, like ``_SafeFormat``).
Templates use the same event and previous-step context as execution. Pure
date/time outputs are previewed; outputs requiring provider calls remain
unresolved until supplied through a per-step test. Because missing tokens render empty, the dry-run re-checks
each rendered step against the registry's field rules (save time only sees
the literal): required fields that render empty and typed fields that render
implausible values are reported as step ``warnings`` — fatal under
``strict``.

``test_step`` narrows both modes to one step (Zapier's per-step "Test
step"): the step runs alone against a sample event — for real with
``execute: true``, render/evaluate only without it — while logic steps are
evaluated, never executed (the branch a run would take, the wait a delay
would take, what a loop would iterate). Prior steps' outputs ride along as
the ``steps`` context, so ``{steps.<id>.output.*}`` templates render against
real data in both the preview and the runner.
"""

import time
import uuid
from datetime import datetime, timezone

from . import matching
from .logic_pkg import RunSuspended

def _supported_action_types():
    """Every type the engine can dispatch, read from the connector registry
    (the single dispatch table). Replaces a hand-mirrored tuple that went
    stale as registry types shipped (code, s3_upload, ... were reported as
    unsupported by dry-runs that could render them fine); anything not
    registered still fails the step exactly as the engine would."""
    from ..connectors import registry

    return set(registry.ACTIONS) | set(registry.LOGIC)

ENVELOPE_KEYS = ("schema_version", "id", "correlation_id", "connector",
                 "event", "source", "occurred_at", "data")


class TestRunError(ValueError):
    """The workflow under test cannot run (e.g. its flow is undefined)."""


def normalize_sample_event(sample, workflow):
    """The event envelope a test run matches against.

    A full envelope (``connector`` + ``event`` both present) is used as-is
    with the engine fields filled in; anything else is the event data,
    wrapped with the workflow's primary trigger connector/event so a plain
    payload matches the workflow under test by default.
    """
    primary = (matching.workflow_triggers(workflow) or [{}])[0]
    if isinstance(sample.get("connector"), str) and isinstance(sample.get("event"), str):
        connector, event_type = sample["connector"], sample["event"]
        data = sample.get("data")
        if not isinstance(data, dict):
            data = {key: value for key, value in sample.items() if key not in ENVELOPE_KEYS}
    else:
        connector = str(primary.get("connector") or "custom")
        event_type = str(primary.get("event") or "received")
        # Reserved envelope keys that appear without both connector and event
        # stay out of the data; a plain payload passes through verbatim.
        data = ({key: value for key, value in sample.items()
                 if key not in ("connector", "event", "data")}
                if {"connector", "event", "data"} & set(sample) else dict(sample))
    event_id = str(sample.get("id") or f"test-{uuid.uuid4()}")
    return {
        "schema_version": "1.0",
        "id": event_id,
        "correlation_id": str(sample.get("correlation_id") or event_id),
        "connector": connector,
        "event": event_type,
        "source": str(sample.get("source") or "test-run"),
        "occurred_at": str(sample.get("occurred_at") or datetime.now(timezone.utc).isoformat()),
        "data": data,
    }


def _report(resolved, envelope, steps, *, mode, error=None):
    return {
        "mode": mode,
        "matched": matching.matches(resolved, envelope),
        "enabled": bool(resolved.get("enabled", True)),
        "event": envelope,
        "trigger": (matching.workflow_triggers(resolved) or [{}])[0],
        "steps": steps,
        "ok": bool(steps) and all(step.get("ok") for step in steps) and error is None,
        "error": error,
    }


def _render_warnings(action, rendered):
    """Field rules re-checked against what this sample rendered.

    The registry's typed-field rules are applied to literals at save time;
    here they run on the *rendered* values, so a required field whose
    template resolves to nothing against this sample (Zapier flags that
    instead of sending blanks) and a value that is no longer plausible for
    its type both come back as warnings. Registered actions only — logic
    steps and unknown types have no field schema to check against.
    """
    from ..connectors import registry

    entry = registry.ACTIONS.get(str(action.get("type") or ""))
    if entry is None or not isinstance(rendered, dict):
        return []
    warnings = []
    for field in entry.fields:
        key = field.get("key")
        if not key:
            continue
        value = rendered.get(key)
        text = "" if value is None else str(value)
        if (key in entry.required or field.get("required")) and not text.strip():
            warnings.append(f"required field '{key}' renders empty against this sample")
            continue
        if not text.strip():
            continue
        problem = registry.field_type_problem(
            field.get("type"), value, choices=field.get("choices") or field.get("options"))
        if problem:
            warnings.append(f"field '{key}' {problem}")
    return warnings


def _dry_run_steps(actions, envelope, step_outputs, *, strict=False, prefix="", workflow_id=""):
    from . import logic_pkg as logic

    steps = []
    for index, action in enumerate(actions or []):
        if not isinstance(action, dict):
            steps.append({"action_id": logic._step_id(prefix, {}, index), "action_type": "", "ok": False,
                          "rendered_input": None, "error": "every action needs a type"})
            continue
        action_id = logic._step_id(prefix, action, index)
        action_type = str(action.get("type") or "")
        step = {
            "action_id": action_id,
            "action_type": action_type,
        }
        try:
            rendered = _render_step_preview(action, envelope, step_outputs)
        except Exception as exc:
            steps.append({**step, "ok": False, "rendered_input": None,
                          "error": f"template error: {exc}"})
            continue
        if action_type not in _supported_action_types():
            step["ok"] = False
            step["error"] = f"unsupported action: {action_type}"
        else:
            step["ok"] = True
        warnings = _render_warnings(action, rendered)
        if warnings:
            step["warnings"] = warnings
            if strict and step["ok"]:
                step["ok"] = False
                step["error"] = warnings[0]
        step["rendered_input"] = rendered
        steps.append(step)
        branch_steps = []
        if step["ok"] and action_type == "date_time":
            # Pure formatting/clock lookup: safe to preview without provider calls.
            from .actions.date_time import run_date_time
            try:
                step["output"] = run_date_time(action, envelope, steps=step_outputs)
            except Exception as exc:
                step["ok"] = False
                step["error"] = str(exc)
        elif step["ok"] and action_type == "condition":
            rules = logic.predicate_rules(action)
            if rules is not None:
                try:
                    passed = logic.evaluate_rules(
                        rules, {**(envelope.get("data") or {}), "steps": step_outputs})
                except Exception as exc:
                    step["ok"] = False
                    step["error"] = str(exc)
                else:
                    branch = "then" if passed else "else"
                    body = action.get(branch) or []
                    if isinstance(body, list) and body:
                        branch_steps = _dry_run_steps(
                            body, envelope, step_outputs, strict=strict,
                            prefix=f"{action_id}.{branch}", workflow_id=workflow_id)
                    summary = logic._collect_branch_summary(f"{action_id}.{branch}", step_outputs)
                    step["output"] = {
                        "condition": "passed" if passed else "failed",
                        "branch": branch,
                        "steps": len(body) if isinstance(body, list) else 0,
                        **summary,
                    }
                    step["branch_steps"] = branch_steps
        elif step["ok"] and action_type == "paths":
            branches = action.get("paths") if isinstance(action.get("paths"), list) else []
            data = {**(envelope.get("data") or {}), "steps": step_outputs}
            matched = None
            branch_name = None
            body = []
            try:
                for cand in branches:
                    if not isinstance(cand, dict):
                        continue
                    rules = logic.predicate_rules(cand)
                    if rules is not None and logic.evaluate_rules(rules, data):
                        matched = str(cand.get("label") or "").strip() or "path"
                        branch_name = matched
                        body = cand.get("actions") if isinstance(cand.get("actions"), list) else []
                        break
                if matched is None:
                    default_steps = (action.get("default")
                                     if action.get("default") is not None
                                     else action.get("else"))
                    if isinstance(default_steps, list) and default_steps:
                        branch_name = "default"
                        body = default_steps
            except Exception as exc:
                step["ok"] = False
                step["error"] = str(exc)
            else:
                if branch_name and body:
                    branch_steps = _dry_run_steps(
                        body, envelope, step_outputs, strict=strict,
                        prefix=f"{action_id}.{branch_name}", workflow_id=workflow_id)
                summary = (logic._collect_branch_summary(f"{action_id}.{branch_name}", step_outputs)
                           if branch_name else {"last_step_id": None, "result": {}, "outputs": {}})
                step["output"] = {
                    "matched": matched,
                    "branch": branch_name,
                    "ran": len(body),
                    **summary,
                }
                step["branch_steps"] = branch_steps
        elif step["ok"] and action_type == "decision":
            try:
                mode, choices = logic._decision_plan(action, action_id)
            except Exception as exc:
                step["ok"] = False
                step["error"] = str(exc)
            else:
                if mode == "switch":
                    picked = choices[0] if choices else None
                    for case in action.get("cases") or []:
                        if isinstance(case, dict) and isinstance(case.get("actions"), list) and case["actions"]:
                            c_label = str(case.get("choice") or "").strip() or "case"
                            sub = _dry_run_steps(
                                case["actions"], envelope, step_outputs, strict=strict,
                                prefix=f"{action_id}.{c_label}", workflow_id=workflow_id)
                            if c_label == picked:
                                branch_steps.extend(sub)
                    summary = (logic._collect_branch_summary(f"{action_id}.{picked}", step_outputs)
                               if picked else {"last_step_id": None, "result": {}, "outputs": {}})
                    step["output"] = {
                        "mode": mode,
                        "choice": picked,
                        "branch": picked,
                        "note": "LLM not consulted — branch previewed",
                        **summary,
                    }
                    step["branch_steps"] = branch_steps
                else:
                    body = action.get("then") if isinstance(action.get("then"), list) else []
                    if body:
                        branch_steps = _dry_run_steps(
                            body, envelope, step_outputs, strict=strict,
                            prefix=f"{action_id}.then", workflow_id=workflow_id)
                    summary = logic._collect_branch_summary(f"{action_id}.then", step_outputs)
                    step["output"] = {
                        "mode": mode,
                        "choice": True,
                        "branch": "then",
                        "note": "LLM not consulted — branch previewed",
                        **summary,
                    }
                    step["branch_steps"] = branch_steps
        elif step["ok"] and action_type == "union":
            _, out, _ = logic._run_union(
                workflow_id, action_id, action, envelope, None,
                step_outputs=step_outputs)
            step["output"] = out

        step_outputs[action_id] = {
            "status": "completed" if step["ok"] else "failed",
            "output": step.get("output", {"dry_run": True}),
        }
        if branch_steps:
            steps.extend(branch_steps)
    return steps


def dry_run(workflow, sample, *, strict=False):
    """Walk the resolved action chain, rendering each step. No side effects:
    provider runners are never executed. Date/time formatting is previewed locally.

    Steps whose rendered inputs trip the registry's field rules collect
    ``warnings``; with ``strict`` those warnings fail the step (``ok`` false,
    ``error`` carrying the first warning) instead of riding along.
    """
    resolved = workflow
    envelope = normalize_sample_event(sample, resolved)
    step_outputs = {}
    steps = _dry_run_steps(
        resolved.get("actions") or [], envelope, step_outputs,
        strict=strict, prefix="", workflow_id=str(resolved.get("id") or ""))
    report = _report(resolved, envelope, steps, mode="dry-run")
    return report


def execute_once(workflow, sample):
    """Run the chain once through the real engine, restricted to this workflow.

    The trigger is forced to match the sample event — the operator asked to
    run this chain against this event; ``matched`` in the payload is what
    reports whether real traffic would have picked the workflow up. A
    failing step stops the chain, exactly as it does in production.
    """
    from . import execute as engine_execute

    resolved = workflow
    envelope = normalize_sample_event(sample, resolved)
    forced = {
        **resolved,
        "enabled": True,
        "triggers": [{"connector": envelope["connector"], "event": envelope["event"]}],
    }
    action_types = {action_id: str(action.get("type") or "")
                    for action_id, action in _iter_steps(resolved.get("actions") or [])}
    steps = []

    def after_action(workflow_id, action_id, event, output=None, duration_ms=None,
                     status="completed"):
        steps.append({"action_id": str(action_id), "action_type": action_types.get(str(action_id), ""),
                      # A step that failed under on_error: continue|run still
                      # reports through after_action — status tells the tale.
                      "ok": status != "failed", "output": output or {}})

    def on_action_error(workflow_id, action_id, event, exc, duration_ms=None):
        steps.append({"action_id": str(action_id), "action_type": action_types.get(str(action_id), ""),
                      "ok": False, "error": str(exc)})

    error = None
    try:
        engine_execute(envelope, after_action=after_action,
                       on_action_error=on_action_error, workflows=[forced])
    except RunSuspended as susp:
        # A delay past the inline sleep cap suspends the run; a test run has
        # nothing to park — the pause is the result, not a failure. The delay
        # step's own entry (status ``delayed``, output carrying resume_at) is
        # already in ``steps`` via the after hook.
        report = _report(resolved, envelope, steps, mode="execute", error=None)
        report["recorded"] = False
        report["suspended"] = True
        report["suspended_until"] = (susp.output or {}).get("resume_at")
        return report
    except Exception as exc:  # a failed step aborts the chain (as in production)
        error = str(exc) or exc.__class__.__name__
    report = _report(resolved, envelope, steps, mode="execute", error=error)
    report["recorded"] = False
    return report


def test_step(workflow, action_id, sample, *, execute=False, step_outputs=None):
    """Test ONE step of the workflow against a sample event (Zapier's
    per-step "Test step" / "Test action").

    The step is addressed by its run-history id (``_iter_steps`` walks the
    action tree with the same ids ``logic.run_chain`` records, so branch and
    error-branch steps like ``route.invoices.0`` or ``send.error.alert`` are
    testable too). A connector action with ``execute: true`` runs through the
    real dispatch — side effects limited to this one step, nothing recorded;
    with ``execute: false`` (the default) nothing is invoked. Logic steps are
    never executed, only evaluated: a filter reports pass/filtered, a
    condition or paths reports the branch a run would take, a delay reports
    the wait it would take without sleeping, a for_each reports what it would
    iterate without running its body — test the body's steps individually.

    ``step_outputs`` is the prior steps' context, shaped like the run history
    (``{action_id: {"status": ..., "output": ...}}`` — a full test-run or a
    previous per-step test provides it), so ``{steps.<id>.output.*}``
    templates render against real data in both the preview and the runner.
    The report is a ``test_run`` report with exactly one step in ``steps``.
    """
    from ..connectors import registry

    resolved = workflow
    envelope = normalize_sample_event(sample, resolved)
    wanted = str(action_id or "").strip()
    if not wanted:
        raise TestRunError("name the step to test: its action id")
    step = find_step(resolved, wanted)
    if step is None:
        raise TestRunError(f"no step named '{wanted}' in workflow '{resolved.get('id')}'")

    step_type = str(step.get("type") or "")
    steps_context = dict(step_outputs or {})
    entry = {
        "action_id": wanted,
        "action_type": step_type,
        "rendered_input": _render_step_preview(step, envelope, steps_context),
    }
    warnings = _render_warnings(step, entry["rendered_input"])
    if warnings:
        entry["warnings"] = warnings

    if not step_type:
        entry["ok"] = False
        entry["error"] = "every action needs a type"
    elif step_type in registry.ACTIONS:
        if execute:
            started = time.monotonic()
            try:
                output = registry.run_action(step, envelope, str(resolved.get("id")),
                                             steps=steps_context)
                entry["output"] = output or {}
                entry["ok"] = True
            except Exception as exc:
                entry["ok"] = False
                entry["error"] = str(exc) or exc.__class__.__name__
            entry["duration_ms"] = int((time.monotonic() - started) * 1000)
        else:
            entry["ok"] = True
    else:
        _evaluate_logic_step(step, step_type, envelope,
                             _template_context(envelope, steps_context),
                             steps_context, entry,
                             workflow_id=str(resolved.get("id")))

    report = _report(resolved, envelope, [entry],
                     mode="execute-step" if execute else "test-step")
    report["action_id"] = wanted
    return report


def _evaluate_logic_step(step, step_type, envelope, context,
                         steps_context, entry, workflow_id=""):
    """What one logic step would do, evaluated without executing it.

    Logic steps carry no side effects of their own — the risk sits in their
    sub-steps, which a per-step test deliberately does not run. The report
    says what a real run would decide here: the branch a condition or paths
    step takes, whether a filter passes, how long a delay waits, what a loop
    would iterate. ``context`` is the template context (the delay's duration
    fields render against it), ``steps_context`` rides along for shape
    symmetry with the connector path, and ``workflow_id`` scopes the digest
    peek.
    """
    from . import logic_pkg as logic

    data = {**(envelope.get("data") or {}), "steps": steps_context or {}}
    if step_type == "filter":
        try:
            stop, output, status = logic._run_filter(step, data)
        except ValueError as exc:
            entry["ok"] = False
            entry["error"] = str(exc)
            return
        entry["ok"] = stop is None
        entry["status"] = status
        entry["output"] = output
        if stop:
            entry["error"] = "filter stops the chain: the sample does not match this step's rules"
    elif step_type == "condition":
        try:
            rules = logic.predicate_rules(step)
            if rules is None:
                raise ValueError(f"condition '{step.get('id', '')}' needs a when mapping or a field")
            passed = logic.evaluate_rules(rules, data)
        except ValueError as exc:
            entry["ok"] = False
            entry["error"] = str(exc)
            return
        branch = "then" if passed else "else"
        body = step.get(branch) or []
        entry["ok"] = True
        entry["status"] = "completed"
        entry["output"] = {"condition": "passed" if passed else "failed",
                           "branch": branch, "steps": len(body) if isinstance(body, list) else 0,
                           "note": "branch not executed — test its steps individually"}
    elif step_type == "paths":
        try:
            branches = step.get("paths")
            if not isinstance(branches, list) or not branches:
                raise ValueError(f"paths '{step.get('id', '')}' needs a non-empty list of paths")
            matched = None
            default_steps = step.get("default") if step.get("default") is not None else step.get("else")
            body = default_steps or []
            for branch in branches:
                rules = logic.predicate_rules(branch)
                if rules is None:
                    raise ValueError(
                        f"paths '{step.get('id', '')}': path "
                        f"'{(branch or {}).get('label', '') if isinstance(branch, dict) else ''}' "
                        "needs a when mapping or a field")
                if logic.evaluate_rules(rules, data):
                    matched = str((branch.get("label") or "").strip() or "path")
                    body = branch.get("actions") or []
                    break
        except ValueError as exc:
            entry["ok"] = False
            entry["error"] = str(exc)
            return
        entry["ok"] = True
        entry["status"] = "completed"
        entry["output"] = {"matched": matched, "steps": len(body) if isinstance(body, list) else 0,
                           "note": "branch not executed — test its steps individually"}
    elif step_type == "decision":
        try:
            mode, choices = logic._decision_plan(step, step.get("id", ""))
            if mode == "switch":
                cases = step.get("cases")
                if cases is not None:
                    if not isinstance(cases, list):
                        raise ValueError(f"decision '{step.get('id', '')}' cases must be a list")
                    for position, case in enumerate(cases):
                        if not isinstance(case, dict):
                            raise ValueError(
                                f"decision '{step.get('id', '')}': case {position} must be a mapping")
                        choice = str(case.get("choice") or "").strip()
                        if choice not in choices:
                            raise ValueError(
                                f"decision '{step.get('id', '')}': case {position} choice "
                                f"'{choice}' is not one of {', '.join(choices)}")
                        if not isinstance(case.get("actions") or [], list):
                            raise ValueError(
                                f"decision '{step.get('id', '')}': case '{choice}' "
                                "actions must be a list of steps")
                for fallback_key in ("default_actions", "default", "else"):
                    if step.get(fallback_key) is not None and not isinstance(step.get(fallback_key), list):
                        raise ValueError(
                            f"decision '{step.get('id', '')}' {fallback_key} must be a list of steps")
            else:
                for branch in ("then", "else"):
                    if step.get(branch) is not None and not isinstance(step[branch], list):
                        raise ValueError(
                            f"decision '{step.get('id', '')}' {branch} must be a list of steps")
        except ValueError as exc:
            entry["ok"] = False
            entry["error"] = str(exc)
            return
        entry["ok"] = True
        entry["status"] = "completed"
        entry["output"] = {"mode": mode, "choice": None, "branch": None,
                           "note": "LLM not consulted — branch not executed"}
    elif step_type == "union":
        try:
            _, output, status = logic._run_union(
                workflow_id, entry.get("action_id", step.get("id", "")),
                step, envelope, None, step_outputs=steps_context)
        except ValueError as exc:
            entry["ok"] = False
            entry["error"] = str(exc)
            return
        entry["ok"] = True
        entry["status"] = status
        entry["output"] = output
    elif step_type == "delay":
        try:
            total, until = logic._delay_request(step, context, step.get("id", ""))
        except ValueError as exc:
            entry["ok"] = False
            entry["error"] = str(exc)
            return
        if until is not None:
            total = max(0.0, until - time.time())
        entry["ok"] = True
        entry["status"] = "completed"
        if total <= logic.MAX_DELAY_SECONDS:
            entry["output"] = {"delay_seconds": round(total, 3), "slept_seconds": 0,
                               "note": "not slept — a step test never waits"}
        else:
            resume_at = time.time() + total
            entry["output"] = {"delay_seconds": round(total, 3), "suspended": True,
                               "resume_at": logic._iso(resume_at),
                               "note": "not slept — a real run suspends until resume_at"}
    elif step_type == "for_each":
        label = step.get("id", "")
        body = step.get("actions")
        if not isinstance(body, list) or not body:
            entry["ok"] = False
            entry["error"] = f"for_each '{label}' needs at least one step in actions"
            return
        path = str(step.get("list") or "").strip().strip("{}").strip()
        if not path:
            entry["ok"] = False
            entry["error"] = f"for_each '{label}': needs a list field"
            return
        items, found = logic._lookup(data, path)
        if not found:
            entry["ok"] = False
            entry["error"] = f"for_each '{label}': list field '{path}' not found in the event data"
            return
        if not isinstance(items, list):
            entry["ok"] = False
            entry["error"] = f"for_each '{label}': field '{path}' is not a list"
            return
        cap = max(0, min(int(step.get("max_iterations") or logic.MAX_LOOP_ITERATIONS),
                         logic.MAX_LOOP_ITERATIONS))
        entry["ok"] = True
        entry["status"] = "completed"
        entry["output"] = {"items": len(items), "iterated": 0, "skipped": 0,
                           "truncated": len(items) > cap,
                           "item_var": str(step.get("item") or "item").strip() or "item",
                           "note": "loop body not executed — test its steps individually"}
    elif step_type == "digest":
        key = str(step.get("key") or "").strip()
        if not key:
            entry["ok"] = False
            entry["error"] = f"digest '{step.get('id', '')}' needs a key"
            return
        output = {"mode": str(step.get("mode") or "accumulate").strip().lower() or "accumulate",
                  "key": key,
                  "note": "not executed — a step test never adds or flushes"}
        try:
            from .actions import digest as digest_actions

            # An informative peek at the batch the step would touch; the
            # store may be unreachable in a test run, which keeps the count
            # out of the report instead of failing the step.
            output["pending"] = digest_actions._pending_count(
                str(workflow_id), digest_actions._prefix(key))
        except Exception:
            pass
        entry["ok"] = True
        entry["status"] = "completed"
        entry["output"] = output
    else:
        entry["ok"] = False
        entry["error"] = f"unsupported action: {step_type}"


def _render_step_preview(step, envelope, steps_context):
    """The step's inputs as its runner would render them.

    Every string leaf goes through templating.render — the same function the
    runners apply to their fields — so ``{steps.<id>.output.*}``,
    ``{trigger.*}`` and formatters preview exactly as they would run, and a
    required field whose template resolves empty collects the same warnings
    the full dry-run raises. The ``code`` key is never templated (the code
    runners execute it verbatim), so a step test previews code bodies intact.
    """
    from .actions import templating

    def walk(value, key=None):
        if key == "code":
            return value
        if isinstance(value, str):
            return templating.render(value, envelope, steps_context)
        if isinstance(value, dict):
            return {item_key: walk(item, item_key) for item_key, item in value.items()}
        if isinstance(value, list):
            return [walk(item) for item in value]
        return value

    return walk(step)


def _template_context(envelope, step_outputs):
    """The template context one step test renders against.

    The same shape the runners template against (templating.build_context):
    the sample's data at the top, the envelope mirrored under ``trigger``,
    and the prior steps' outputs under ``steps``.
    """
    from .actions.templating import build_context

    return build_context(envelope, step_outputs or {})


def _iter_steps(steps, prefix=""):
    """Every (run-history id, step) pair in the action tree.

    The ids are exactly what ``logic.run_chain`` records at run time:
    top-level ``id`` (or index), ``<condition>.then|.else.<sub>``,
    ``<paths>.<label>.<sub>``, ``<for_each>[0].<sub>`` (the first iteration's
    prefix — a loop body step is testable against the sample data directly),
    and ``<step>.error.<sub>`` for error branches.
    """
    from . import logic_pkg as logic

    for index, step in enumerate(steps or []):
        if not isinstance(step, dict):
            continue
        action_id = logic._step_id(prefix, step, index)
        yield action_id, step
        kind = str(step.get("type") or "")
        if kind == "condition":
            for branch in ("then", "else"):
                if isinstance(step.get(branch), list):
                    yield from _iter_steps(step[branch], f"{action_id}.{branch}")
        elif kind == "paths":
            for branch in step.get("paths") or []:
                if isinstance(branch, dict):
                    label = str(branch.get("label") or "").strip() or "path"
                    if isinstance(branch.get("actions"), list):
                        yield from _iter_steps(branch["actions"], f"{action_id}.{label}")
            default_steps = step.get("default") if step.get("default") is not None else step.get("else")
            if isinstance(default_steps, list):
                yield from _iter_steps(default_steps, f"{action_id}.default")
        elif kind == "decision":
            if logic._decision_mode(step) == "switch":
                for case in step.get("cases") or []:
                    if isinstance(case, dict) and isinstance(case.get("actions"), list):
                        label = str(case.get("choice") or "").strip() or "case"
                        yield from _iter_steps(case["actions"], f"{action_id}.{label}")
                for fallback_key in ("default_actions", "default", "else"):
                    if isinstance(step.get(fallback_key), list):
                        yield from _iter_steps(step[fallback_key], f"{action_id}.default")
                        break
            else:
                for branch in ("then", "else"):
                    if isinstance(step.get(branch), list):
                        yield from _iter_steps(step[branch], f"{action_id}.{branch}")
        elif kind == "for_each" and isinstance(step.get("actions"), list):
            yield from _iter_steps(step["actions"], f"{action_id}[0]")
        if isinstance(step.get("error_actions"), list):
            yield from _iter_steps(step["error_actions"], f"{action_id}.error")


def find_step(workflow, action_id):
    """The step a run would record under ``action_id``, or None."""
    resolved = workflow
    wanted = str(action_id or "").strip()
    for candidate, step in _iter_steps(resolved.get("actions") or []):
        if candidate == wanted:
            return step
    return None


def test_run(workflow, sample, *, execute=False, strict=False):
    """Dry-run (default) or execute one workflow against a sample event.

    ``strict`` applies to the dry-run: rendered-input warnings (a required
    field the sample renders empty, a value implausible for its declared
    type) fail their step instead of riding along. Execute mode ignores it —
    the real engine fails loudly on its own.
    """
    return execute_once(workflow, sample) if execute else dry_run(workflow, sample, strict=strict)
