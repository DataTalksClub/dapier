"""Workflow engine: match events to workflows and run their actions."""
from .matching import (  # noqa: F401
    all_workflows,
    flow_actions,
    flow_catalog,
    flows,
    matches,
    resolve_workflow,
    workflow_triggers,
    workflows,
)
from .actions.webhook import run_webhook  # noqa: F401
from .actions.slack import run_slack  # noqa: F401
from .actions.telegram import run_telegram_send  # noqa: F401
from .actions.email import run_email_send  # noqa: F401
from .actions.dataops import run_dataops  # noqa: F401
from .actions.dropbox import run_dropbox_delete, run_dropbox_upload  # noqa: F401
from .actions.render import run_render_job  # noqa: F401
from .actions.code import run_code  # noqa: F401
from .logic import run_chain


def _run_connector(action, event, workflow_id, steps=None):
    """Run one connector action; the engine's dispatch table. ``steps`` is
    the run's accumulated step outputs, for the templating runners."""
    if action["type"] == "webhook":
        return run_webhook(action, event)
    if action["type"] == "slack":
        return run_slack(action, event, steps=steps)
    if action["type"] == "telegram_send":
        return run_telegram_send(action, event, steps=steps)
    if action["type"] == "email_send":
        return run_email_send(action, event, steps=steps)
    if action["type"] == "dataops":
        return run_dataops(action, event)
    if action["type"] == "dropbox_upload":
        return run_dropbox_upload(action, event)
    if action["type"] == "dropbox_delete":
        return run_dropbox_delete(action, event)
    if action["type"] == "render_html_to_pdf":
        return run_render_job(action, event, workflow_id)
    if action["type"] == "code":
        return run_code(action, event)
    raise ValueError(f"unsupported action: {action['type']}")


def execute(event, before_action=None, after_action=None, on_action_error=None):
    """Run every matching workflow's actions.

    The chain may mix connector actions with logic steps (filter, condition,
    delay, for_each — see engine.logic). The hooks carry the step telemetry:
    ``before_action`` also gets the action type, ``after_action`` gets the
    runner's output summary, the step duration, and the terminal status
    (``completed`` or ``filtered``), and ``on_action_error`` gets the duration
    of the failed attempt. Runners return a small JSON-safe dict describing
    what happened (message ids, paths, HTTP statuses) — it lands on the run
    record.

    Step outputs accumulate per workflow run in a ``steps`` mapping shaped
    like the run history (``{action_id: {"status": ..., "output": ...}}``);
    the templating runners receive it so later steps can reference earlier
    ones: ``{steps.<action_id>.output.<path>}``, ``{steps.<action_id>.status}``.
    """
    for workflow in all_workflows():
        if matches(workflow, event):
            run_chain(
                workflow["id"], workflow.get("actions", []), event,
                _run_connector,
                before_action=before_action,
                after_action=after_action,
                on_action_error=on_action_error,
            )
