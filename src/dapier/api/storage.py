"""Workflow storage: read/write the per-workflow key-value state over HTTP.

Both API surfaces expose the same four behaviors for a workflow's storage
partition (the same table and rules the ``storage_*`` actions run under —
see ``engine/actions/storage.py``, whose ``kv_*`` helpers are the domain
implementation):

- ``GET  …/storage/{workflow}?key=…``      reads one value;
- ``GET  …/storage/{workflow}?prefix=…``   lists keys under a prefix;
- ``POST …/storage/{workflow}``            ``{key, value, ttl_seconds}`` stores;
- ``DELETE …/storage/{workflow}?key=…``    removes one key.

``api.agent`` and ``api.admin.routes`` are thin wrappers that add their own
authentication; domain logic lives only here so the console, the CLI and
the engine cannot drift apart.
"""
from ..engine.actions import storage


def _error(exc):
    """A ValueError from the domain layer is a client error (400)."""
    return 400, {"error": str(exc)}


def get(workflow_id, key):
    try:
        item = storage.kv_get(workflow_id, key)
    except ValueError as exc:
        return _error(exc)
    if item is None:
        return 404, {"error": f"No stored value for key '{key or ''}'"}
    return 200, {
        "workflow": workflow_id,
        "key": item.get("key"),
        "value": str(item.get("value") or ""),
        "updated_at": item.get("updated_at"),
        "expires": item.get("expires"),
    }


def find(workflow_id, prefix, limit):
    try:
        parsed = int(limit) if str(limit or "").strip() else None
    except (TypeError, ValueError):
        return 400, {"error": "limit must be a whole number"}
    try:
        items = storage.kv_find(workflow_id, prefix or "", parsed)
    except ValueError as exc:
        return _error(exc)
    return 200, {
        "workflow": workflow_id,
        "prefix": prefix or "",
        "items": [
            {
                "key": str(item.get("key") or ""),
                "value": str(item.get("value") or ""),
                "updated_at": item.get("updated_at"),
            }
            for item in items
        ],
        "count": len(items),
    }


def set_value(workflow_id, payload):
    body = payload if isinstance(payload, dict) else {}
    try:
        item = storage.kv_set(
            workflow_id,
            body.get("key"),
            body.get("value"),
            body.get("ttl_seconds"),
        )
    except ValueError as exc:
        return _error(exc)
    result = {
        "workflow": workflow_id,
        "key": item.get("key"),
        "stored": True,
        "updated_at": item.get("updated_at"),
    }
    if item.get("expires") is not None:
        result["expires"] = item.get("expires")
    return 200, result


def delete(workflow_id, key):
    try:
        deleted = storage.kv_delete(workflow_id, key)
    except ValueError as exc:
        return _error(exc)
    return 200, {"workflow": workflow_id, "key": key, "deleted": deleted}
