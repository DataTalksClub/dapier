"""Per-workflow key/value storage endpoints."""
from ... import http
import json
from .. import storage as storage_api
from .gates import _save_denied, _write_denied


def storage_read(event, workflow_id, visible=None):
    """Workflow storage: one key (``key=``) or the keys under ``prefix=``.

    ``visible`` scopes the reads exactly as the writes gate: a hidden
    partition answers like an empty one."""
    query = event.get("queryStringParameters") or {}
    key = str(query.get("key") or "").strip()
    if key:
        status, payload = storage_api.get(workflow_id, key, visible=visible)
    else:
        status, payload = storage_api.find(workflow_id, query.get("prefix"),
                                           query.get("limit"), visible=visible)
    return http._json_response(status, payload)


def storage_write(event, workflow_id, visible=None, operator=None):
    """Store one workflow storage value: ``{key, value, ttl_seconds}``."""
    denied = _write_denied(visible, workflow_id, "storage.write", operator)
    if denied:
        return denied
    try:
        body = json.loads(event.get("body") or "{}")
    except ValueError:
        return http._json_response(400, {"error": "Body must be JSON"})
    status, payload = storage_api.set_value(workflow_id, body)
    return http._json_response(status, payload)


def storage_delete(event, workflow_id, visible=None, operator=None):
    """Remove one workflow storage value: ``?key=``."""
    denied = _write_denied(visible, workflow_id, "storage.write", operator)
    if denied:
        return denied
    query = event.get("queryStringParameters") or {}
    status, payload = storage_api.delete(workflow_id, query.get("key"))
    return http._json_response(status, payload)


