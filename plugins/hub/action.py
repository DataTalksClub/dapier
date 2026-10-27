"""Authenticated Hub session action; credentials live in the team store."""
import json
import os
import urllib.error
import urllib.request
from urllib.parse import urlsplit

from src.dapier.engine.actions import storage
from src.dapier.triggers.intake import developer_webhook

from .session import DynamoJournal, launch, request_key, _response, _uuid


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, message, headers, newurl):
        return None


def _transport(method, url, *, headers, body, timeout=15):
    """Never forward the service bearer to a redirect destination."""
    request = urllib.request.Request(url, data=body, headers=headers, method=method)
    opener = urllib.request.build_opener(_NoRedirect())
    try:
        with opener.open(request, timeout=timeout) as response:
            status, raw = response.status, response.read()
    except urllib.error.HTTPError as exc:
        status, raw = exc.code, exc.read()
    try:
        parsed = json.loads(raw) if raw else {}
    except (ValueError, UnicodeDecodeError):
        parsed = {}
    return status, parsed


def service_token():
    name = str(os.environ.get("HUB_INTAKE_TOKEN_SECRET_NAME") or "").strip()
    if not name:
        raise ValueError("Hub session action needs HUB_INTAKE_TOKEN_SECRET_NAME")
    # Lazy import keeps catalog loading independent of configured credentials.
    # No AWS Secrets Manager or environment-token fallback is permitted.
    from src.dapier.secrets import value

    raw = value(name)
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        token = raw
    else:
        token = (parsed.get("token") or parsed.get("access_token")) if isinstance(parsed, dict) else parsed
    if not isinstance(token, str) or not token.strip() or any(c.isspace() for c in token.strip()):
        raise ValueError("Hub service credential must contain a bearer token")
    return token.strip()


def hub_endpoint():
    endpoint = developer_webhook.hub_intake_url()
    parsed = urlsplit(endpoint)
    if parsed.scheme != "https" or not parsed.netloc or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("Hub intake URL must be an HTTPS endpoint without credentials, query or fragment")
    return endpoint


def get_hub_task(task_id):
    task_id = _uuid(task_id, "task id")
    status, body = _transport("GET", hub_endpoint() + "/" + task_id,
                              headers={"authorization": f"Bearer {service_token()}",
                                       "user-agent": "dapier-hub/1"}, body=None, timeout=3)
    response = _response(status, body, "read")
    if not isinstance(response.get("task"), dict):
        raise RuntimeError("Hub read returned no task")
    return response["task"]


def run_hub_session(action, event, workflow_id, steps=None):
    from src.dapier.engine.logic_pkg.execution import current_action_id
    from .completions import CompletionStore

    if not isinstance(action.get("id"), str) or not action["id"].strip():
        raise ValueError("hub_session requires non-empty id")
    action = {**action, "id": current_action_id() or action.get("id")}
    endpoint = hub_endpoint()
    table = storage._table()
    journal = DynamoJournal(table, workflow_id)
    token = None

    def post(path, payload):
        nonlocal token
        if token is None:
            token = service_token()
        return developer_webhook.create_hub_intake(payload, token=token, url=endpoint + path,
                                                  transport=_transport)

    result = launch(action, event, workflow_id, journal=journal, post=post, steps=steps)
    request = journal.get(request_key(workflow_id, event.get("id"), action["id"]) + "/request") or {}
    metadata = request.get("completion") or {
        "notify_to": "", "notify_from": "agents@dtcdev.click", "title": "Hub session",
        "source_workflow_id": workflow_id, "source_event_id": event["id"], "step_id": action["id"]}
    CompletionStore(table).enqueue({**metadata, "hub_task_id": result["hub_task_id"],
                                    "hub_session_id": result["hub_session_id"],
                                    "hub_link": result.get("hub_link") or f"https://hub.dtcdev.click/#/intake/{result['hub_task_id']}"})
    return result
