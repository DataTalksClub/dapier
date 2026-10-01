"""Host task and worker presence endpoints."""
from ... import host_tasks
from ... import host_workers
from ... import http


def agent_tasks_list(event):
    """Host tasks the agent action enqueued (status, error, timestamps).

    Read-only: the session/role gate in the dispatcher covers it, and like
    the other list reads the read itself is not audited.
    """
    query = event.get("queryStringParameters") or {}
    if query.get("task_id"):
        status, payload = host_tasks.api_get(query["task_id"])
    else:
        status, payload = host_tasks.api_list(
            limit=query.get("limit"), status=query.get("status"))
    return http._json_response(status, payload)


def workers_list(event):
    """Host workers (`dapier worker` processes) and their presence.

    Read-only: the session/role gate in the dispatcher covers it, and like
    the other list reads the read itself is not audited. The same rows feed
    `dapier workers list` through /api/agent/workers.
    """
    return http._json_response(*host_workers.api_list())


