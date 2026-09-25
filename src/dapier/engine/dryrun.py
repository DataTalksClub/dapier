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
Accumulated outputs are exposed under each step's action id; the engine
today passes only the event data forward, so the event data is the faithful
template source.
"""

import uuid
from datetime import datetime, timezone

from . import matching

# Action types the engine dispatcher knows; anything else fails at run time.
RUNNER_TYPES = (
    "webhook",
    "slack",
    "telegram_send",
    "email_send",
    "dataops",
    "dropbox_upload",
    "dropbox_delete",
    "render_html_to_pdf",
)

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


class _SafeContext(dict):
    """format_map context: unknown tokens render empty, like the runners."""

    def __missing__(self, key):
        return ""


def _render_value(value, context):
    if isinstance(value, str):
        return value.format_map(_SafeContext(context))
    if isinstance(value, dict):
        return {key: _render_value(item, context) for key, item in value.items()}
    if isinstance(value, list):
        return [_render_value(item, context) for item in value]
    return value


def _resolved_or_error(workflow):
    resolved = matching.resolve_workflow(workflow)
    if resolved is None:
        raise TestRunError(f"no shared flow named '{workflow.get('flow')}'")
    return resolved


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


def dry_run(workflow, sample):
    """Walk the resolved action chain, rendering each step. No side effects:
    the real runners are never imported for execution, let alone invoked."""
    resolved = _resolved_or_error(workflow)
    envelope = normalize_sample_event(sample, resolved)
    context = dict(envelope["data"])
    steps = []
    for index, action in enumerate(resolved.get("actions") or []):
        if not isinstance(action, dict):
            steps.append({"action_id": str(index), "action_type": "", "ok": False,
                          "rendered_input": None, "error": "every action needs a type"})
            continue
        step = {
            "action_id": str(action.get("id", index)),
            "action_type": str(action.get("type") or ""),
        }
        try:
            step["rendered_input"] = _render_value(action, context)
        except Exception as exc:
            steps.append({**step, "ok": False, "rendered_input": None,
                          "error": f"template error: {exc}"})
            continue
        if step["action_type"] not in RUNNER_TYPES:
            step["ok"] = False
            step["error"] = f"unsupported action: {step['action_type']}"
        else:
            step["ok"] = True
        steps.append(step)
        # Placeholder output for later steps' context; the engine passes only
        # the event data forward today, so real outputs never feed templates.
        context[step["action_id"]] = {"dry_run": True}
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

    resolved = _resolved_or_error(workflow)
    envelope = normalize_sample_event(sample, resolved)
    forced = {
        **resolved,
        "enabled": True,
        "triggers": [{"connector": envelope["connector"], "event": envelope["event"]}],
    }
    action_types = {str(action.get("id", index)): str(action.get("type") or "")
                    for index, action in enumerate(resolved.get("actions") or [])
                    if isinstance(action, dict)}
    steps = []

    def after_action(workflow_id, action_id, event, output=None, duration_ms=None):
        steps.append({"action_id": str(action_id), "action_type": action_types.get(str(action_id), ""),
                      "ok": True, "output": output or {}})

    def on_action_error(workflow_id, action_id, event, exc, duration_ms=None):
        steps.append({"action_id": str(action_id), "action_type": action_types.get(str(action_id), ""),
                      "ok": False, "error": str(exc)})

    error = None
    try:
        engine_execute(envelope, after_action=after_action,
                       on_action_error=on_action_error, workflows=[forced])
    except Exception as exc:  # a failed step aborts the chain (as in production)
        error = str(exc) or exc.__class__.__name__
    report = _report(resolved, envelope, steps, mode="execute", error=error)
    report["recorded"] = False
    return report


def test_run(workflow, sample, *, execute=False):
    """Dry-run (default) or execute one workflow against a sample event."""
    return execute_once(workflow, sample) if execute else dry_run(workflow, sample)
