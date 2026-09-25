"""Workflow engine: match events to workflows and run their actions."""
import time

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


def _elapsed(started):
    return int((time.monotonic() - started) * 1000)


def execute(event, before_action=None, after_action=None, on_action_error=None):
    """Run every matching workflow's actions.

    The hooks carry the step telemetry: ``before_action`` also gets the
    action type, ``after_action`` gets the runner's output summary and the
    step duration, and ``on_action_error`` gets the duration of the failed
    attempt. Runners return a small JSON-safe dict describing what happened
    (message ids, paths, HTTP statuses) — it lands on the run record.
    """
    for workflow in all_workflows():
        if matches(workflow, event):
            for index, action in enumerate(workflow.get("actions", [])):
                action_id = action.get("id", str(index))
                if before_action and not before_action(
                    workflow["id"], action_id, event, action.get("type"),
                ):
                    continue
                started = time.monotonic()
                try:
                    if action["type"] == "webhook":
                        output = run_webhook(action, event)
                    elif action["type"] == "slack":
                        output = run_slack(action, event)
                    elif action["type"] == "telegram_send":
                        output = run_telegram_send(action, event)
                    elif action["type"] == "email_send":
                        output = run_email_send(action, event)
                    elif action["type"] == "dataops":
                        output = run_dataops(action, event)
                    elif action["type"] == "dropbox_upload":
                        output = run_dropbox_upload(action, event)
                    elif action["type"] == "dropbox_delete":
                        output = run_dropbox_delete(action, event)
                    elif action["type"] == "render_html_to_pdf":
                        output = run_render_job(action, event, workflow["id"])
                    elif action["type"] == "code":
                        output = run_code(action, event)
                    else:
                        raise ValueError(f"unsupported action: {action['type']}")
                except Exception as exc:
                    if on_action_error:
                        on_action_error(workflow["id"], action_id, event, exc,
                                        duration_ms=_elapsed(started))
                    raise
                if after_action:
                    after_action(workflow["id"], action_id, event,
                                 output=output or {}, duration_ms=_elapsed(started))
