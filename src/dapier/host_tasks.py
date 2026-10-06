"""Public host task read model shared by the console and CLI."""
import os

# The display projection. ``workflow`` is derived from the task id
# (``agent:<workflow_id>:<event_id>:<action_id>``), not stored.
PUBLIC_FIELDS = (
    "task_id", "kind", "engine", "requires", "workspace", "tag_prefix", "status",
    "session_id", "tag", "error", "created_at", "sent_at",
    "started_at", "finished_at", "exit_code", "summary", "notified_at", "email_subject",
)

DEFAULT_LIMIT = 50
MAX_LIMIT = 200


def tasks_table(table_ref=None):
    if table_ref is not None:
        return table_ref
    import boto3

    return boto3.resource("dynamodb").Table(os.environ["HOST_TASKS_TABLE"])


def workflow_of(task_id):
    """The workflow id inside ``agent:<workflow_id>:<event>:<action>``."""
    parts = str(task_id or "").split(":")
    if len(parts) >= 4 and parts[0] == "agent":
        return parts[1]
    return ""


def api_get(task_id, table_ref=None):
    """One task with recorded output; credentials and the prompt stay private."""
    item = tasks_table(table_ref).get_item(Key={"task_id": str(task_id)}).get("Item")
    if not item:
        return 404, {"error": "Agent task not found"}
    task = {key: item.get(key) for key in PUBLIC_FIELDS}
    task["workflow"] = workflow_of(task_id)
    task["logs"] = item.get("logs")
    return 200, {"task": task}


def api_list(table_ref=None, limit=DEFAULT_LIMIT, status=None):
    """Recent host tasks, newest first: ``(status_code, payload)``.

    A paginated scan; ``status`` narrows to one stored state and
    ``limit`` clips the newest rows. Rows project to PUBLIC_FIELDS only.
    """
    try:
        size = max(1, min(int(limit), MAX_LIMIT))
    except (TypeError, ValueError):
        size = DEFAULT_LIMIT
    table = tasks_table(table_ref)
    items, start = [], None
    while True:
        page = table.scan(**({"ExclusiveStartKey": start} if start else {}))
        items.extend(page.get("Items", []))
        start = page.get("LastEvaluatedKey")
        if not start:
            break
    # Worker presence rows (host_workers.checkin) share the table but are
    # not tasks; the Workers tab reads them through its own list.
    items = [item for item in items if item.get("kind") != "worker"]
    items.sort(key=lambda item: (int(item.get("created_at") or 0),
                                 str(item.get("task_id") or "")), reverse=True)
    wanted = str(status or "").strip().lower()
    if wanted:
        items = [item for item in items
                 if str(item.get("status") or "").strip().lower() == wanted]
    tasks = []
    for item in items[:size]:
        view = {key: item.get(key) for key in PUBLIC_FIELDS}
        view["workflow"] = workflow_of(view["task_id"])
        tasks.append(view)
    return 200, {"tasks": tasks}
