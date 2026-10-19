"""Operator Typefully proxy: uses the write-only credential server-side."""
import json
import re

from ...api.discovery import _pseudo_connection
from .common import _LateBinding, _json_response

require_operator = _LateBinding("require_operator")
audit = _LateBinding("audit")


def _plugin():
    from plugins.typefully import plugin
    return plugin


def _connection():
    return _pseudo_connection("typefully")


def _params(event, extra=None):
    query = dict(event.get("queryStringParameters") or {})
    if extra:
        query.update(extra)
    return query


def _error(exc):
    message = str(exc) or "Typefully request failed"
    # Never echo provider/credential material; keep generic for transport failures.
    if "HTTP" in message or "unreachable" in message or "invalid JSON" in message:
        return _json_response(502, {"error": message})
    return _json_response(400, {"error": message})


def social_sets_api(event):
    subject, error = require_operator(event, "typefully.social_sets")
    if error:
        return error
    try:
        items = _plugin().social_sets(_connection(), _params(event))
    except ValueError as exc:
        return _error(exc)
    audit.emit("typefully", "typefully.social_sets", subject, outcome="ok")
    return _json_response(200, {"items": items, "count": len(items)})


def drafts_api(event):
    subject, error = require_operator(event, "typefully.drafts")
    if error:
        return error
    try:
        payload = _plugin().drafts_export(_connection(), _params(event))
    except ValueError as exc:
        return _error(exc)
    audit.emit("typefully", "typefully.drafts", subject, outcome="ok")
    return _json_response(200, payload)


def draft_api(event, social_set_id, draft_id):
    subject, error = require_operator(event, "typefully.draft")
    if error:
        return error
    try:
        payload = _plugin().get_draft(_connection(), {
            **_params(event), "social_set_id": social_set_id, "draft_id": draft_id})
    except ValueError as exc:
        return _error(exc)
    audit.emit("typefully", "typefully.draft", subject, outcome="ok")
    return _json_response(200, payload)


def queue_api(event, social_set_id):
    subject, error = require_operator(event, "typefully.queue")
    if error:
        return error
    try:
        payload = _plugin().get_queue(_connection(), {
            **_params(event), "social_set_id": social_set_id})
    except ValueError as exc:
        return _error(exc)
    audit.emit("typefully", "typefully.queue", subject, outcome="ok")
    return _json_response(200, payload)


def create_draft_api(event, social_set_id):
    subject, error = require_operator(event, "typefully.create_draft")
    if error:
        return error
    try:
        body = json.loads(event.get("body") or "{}")
    except (ValueError, TypeError):
        return _json_response(400, {"error": "Invalid JSON body"})
    if not isinstance(body, dict):
        return _json_response(400, {"error": "Invalid JSON body"})
    try:
        payload = _plugin().create_unscheduled_draft(_connection(), {
            "social_set_id": social_set_id, "body": body})
    except ValueError as exc:
        return _error(exc)
    audit.emit("typefully", "typefully.create_draft", subject, outcome="ok")
    return _json_response(201, payload)


def route_typefully(event, method, path):
    """Return a response for Typefully agent routes, or None if unmatched."""
    if path == "/api/agent/typefully/social-sets" and method == "GET":
        return social_sets_api(event)
    if path == "/api/agent/typefully/drafts" and method == "GET":
        return drafts_api(event)
    match = re.fullmatch(r"/api/agent/typefully/social-sets/(\d+)/drafts/(\d+)", path)
    if match and method == "GET":
        return draft_api(event, match.group(1), match.group(2))
    match = re.fullmatch(r"/api/agent/typefully/social-sets/(\d+)/queue", path)
    if match and method == "GET":
        return queue_api(event, match.group(1))
    match = re.fullmatch(r"/api/agent/typefully/social-sets/(\d+)/drafts", path)
    if match and method == "POST":
        return create_draft_api(event, match.group(1))
    return None
