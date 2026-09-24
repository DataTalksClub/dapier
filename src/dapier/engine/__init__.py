"""Workflow engine: match events to workflows and run their actions."""
from .matching import all_workflows, matches, workflows  # noqa: F401
from .actions.webhook import run_webhook  # noqa: F401
from .actions.slack import run_slack  # noqa: F401
from .actions.telegram import run_telegram_send  # noqa: F401
from .actions.email import run_email_send  # noqa: F401
from .actions.dataops import run_dataops  # noqa: F401
from .actions.dropbox import run_dropbox_delete, run_dropbox_upload  # noqa: F401
from .actions.render import run_render_job  # noqa: F401


def execute(event, before_action=None, after_action=None, on_action_error=None):
    for workflow in all_workflows():
        if matches(workflow, event):
            for index, action in enumerate(workflow.get("actions", [])):
                action_id = action.get("id", str(index))
                if before_action and not before_action(workflow["id"], action_id, event):
                    continue
                try:
                    if action["type"] == "webhook":
                        run_webhook(action, event)
                    elif action["type"] == "slack":
                        run_slack(action, event)
                    elif action["type"] == "telegram_send":
                        run_telegram_send(action, event)
                    elif action["type"] == "email_send":
                        run_email_send(action, event)
                    elif action["type"] == "dataops":
                        run_dataops(action, event)
                    elif action["type"] == "dropbox_upload":
                        run_dropbox_upload(action, event)
                    elif action["type"] == "dropbox_delete":
                        run_dropbox_delete(action, event)
                    elif action["type"] == "render_html_to_pdf":
                        run_render_job(action, event, workflow["id"])
                    else:
                        raise ValueError(f"unsupported action: {action['type']}")
                except Exception as exc:
                    if on_action_error:
                        on_action_error(workflow["id"], action_id, event, exc)
                    raise
                if after_action:
                    after_action(workflow["id"], action_id, event)
