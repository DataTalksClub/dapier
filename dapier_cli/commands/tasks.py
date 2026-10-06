"""Implementations of the `dapier agent-tasks`, `dapier workers`, and `dapier worker` commands."""

import json
from urllib.parse import quote

from .. import api
from .. import config

__all__ = ["agent_tasks_list", "agent_tasks_show", "worker_run", "workers_list"]


def agent_tasks_list(api_url, debug=False, limit=None, status=None):
    """Host jobs the agent action enqueued, as `dapier worker` leaves them."""
    params = []
    if limit:
        params.append(f"limit={quote(str(limit), safe='')}")
    if status:
        params.append(f"status={quote(status, safe='')}")
    path = "/api/agent/agent-tasks" + ("?" + "&".join(params) if params else "")
    data = api.call(api_url, "GET", path, debug=debug)
    items = data.get("tasks") or []
    if not items:
        print("No host tasks yet. An agent action enqueues one when its workflow runs.")
        return 0
    print(f"{'STATUS':10} {'WORKFLOW':30} TASK ID")
    for item in items:
        print(f"{item.get('status', ''):10} {str(item.get('workflow') or ''):30} "
              f"{item.get('task_id') or ''}")
        if item.get("requires"):
            print("           requires " + ", ".join(item["requires"]))
        if item.get("email_subject"):
            print(f"           task {item['email_subject']}")
        if item.get("summary"):
            print(f"           result {item['summary']}")
        if item.get("error"):
            print(f"           error {item['error']}")
    return 0


def workers_list(api_url, debug=False):
    """Host workers (`dapier worker` processes), most recently seen first."""
    data = api.call(api_url, "GET", "/api/agent/workers", debug=debug)
    workers = data.get("workers") or []
    if not workers:
        print("No worker has checked in yet. Start one with `dapier worker` — "
              "agent tasks stay queued until a worker picks them up.")
        return 0
    print(f"{'STATUS':10} {'WORKER':44} CURRENT TASK")
    for worker in workers:
        state = "active" if worker.get("active") else "offline"
        current = worker.get("current_task_id") or "—"
        print(f"{state:10} {str(worker.get('worker_id') or ''):44} {current}")
        where = " @ ".join(part for part in (worker.get("hostname"),
                                             worker.get("workspace_root")) if part)
        if where:
            print(f"           {where}")
        print(f"           {worker.get('engine') or 'claude'}; capabilities: "
              + (", ".join(worker.get("capabilities") or []) or "none"))
        if worker.get("last_task_id"):
            print(f"           last {worker['last_task_id']}"
                  f" -> {worker.get('last_status') or 'unknown'}")
    return 0


def agent_tasks_show(api_url, task_id, debug=False):
    data = api.call(api_url, "GET", "/api/agent/agent-tasks?task_id=" + quote(task_id, safe=""),
                    debug=debug)
    print(json.dumps(data["task"], indent=2, ensure_ascii=False))
    return 0


def worker_run(api_url=None, *, token_file=None, workspace_root=None,
               max_runtime=3600, once=False, capabilities=(), engine="claude"):
    """Run headless jobs through the authenticated HTTPS host API."""
    from src.dapier.headless_worker import DEFAULT_ROOT, DEFAULT_TOKEN_FILE, serve

    serve(api_url=api_url or config.api_url(),
          token_file=token_file or DEFAULT_TOKEN_FILE,
          workspace_root=workspace_root or DEFAULT_ROOT,
          max_runtime=max_runtime, once=once, capabilities=capabilities, engine=engine)
    return 0
