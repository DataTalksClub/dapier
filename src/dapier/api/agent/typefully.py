"""Operator Typefully proxy: uses the write-only credential server-side."""
import base64
import binascii
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
    params = _params(event)
    allow_schedule = str(params.get("allow_schedule") or "").strip().lower() in ("1", "true", "yes")
    try:
        if allow_schedule:
            payload = _plugin().create_scheduled_draft(_connection(), {
                "social_set_id": social_set_id, "body": body})
            audit.emit("typefully", "typefully.schedule_draft", subject, outcome="ok")
        else:
            payload = _plugin().create_unscheduled_draft(_connection(), {
                "social_set_id": social_set_id, "body": body})
            audit.emit("typefully", "typefully.create_draft", subject, outcome="ok")
    except ValueError as exc:
        return _error(exc)
    return _json_response(201, payload)


def patch_draft_api(event, social_set_id, draft_id):
    subject, error = require_operator(event, "typefully.patch_draft")
    if error:
        return error
    try:
        body = json.loads(event.get("body") or "{}")
    except (ValueError, TypeError):
        return _json_response(400, {"error": "Invalid JSON body"})
    if not isinstance(body, dict):
        return _json_response(400, {"error": "Invalid JSON body"})
    try:
        payload = _plugin().patch_draft(_connection(), {
            "social_set_id": social_set_id, "draft_id": draft_id, "body": body})
    except ValueError as exc:
        return _error(exc)
    audit.emit("typefully", "typefully.patch_draft", subject, outcome="ok")
    return _json_response(200, payload)


def _redact_media_result(result):
    """Keep only identifier/status fields; signed URLs never leave the server."""
    if not isinstance(result, dict):
        return {}
    return {key: result[key] for key in ("media_id", "file_name", "status", "error_reason")
            if key in result}


def upload_media_api(event, social_set_id):
    subject, error = require_operator(event, "typefully.upload_media")
    if error:
        return error
    try:
        body = json.loads(event.get("body") or "{}")
    except (ValueError, TypeError):
        return _json_response(400, {"error": "Invalid JSON body"})
    if not isinstance(body, dict):
        return _json_response(400, {"error": "Invalid JSON body"})
    file_name = body.get("file_name")
    encoded = body.get("content_base64")
    if not isinstance(file_name, str) or not file_name.strip():
        return _json_response(400, {"error": "file_name is required"})
    if not isinstance(encoded, str) or not encoded.strip():
        return _json_response(400, {"error": "content_base64 is required"})
    try:
        content = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError):
        return _json_response(400, {"error": "content_base64 is not valid base64"})
    if not content:
        return _json_response(400, {"error": "content_base64 is empty"})
    if len(content) > _plugin().MAX_MEDIA_BYTES:
        return _json_response(400, {"error": "media file is too large"})
    alt_text = body.get("alt_text")
    if alt_text is not None and not isinstance(alt_text, str):
        return _json_response(400, {"error": "alt_text must be a string"})
    try:
        payload = _plugin().upload_media(_connection(), {
            "social_set_id": social_set_id, "file_name": file_name.strip(),
            "alt_text": alt_text, "content": content})
    except ValueError as exc:
        return _error(exc)
    audit.emit("typefully", "typefully.upload_media", subject, outcome="ok")
    return _json_response(201, _redact_media_result(payload))


def media_status_api(event, social_set_id, media_id):
    subject, error = require_operator(event, "typefully.media_status")
    if error:
        return error
    try:
        payload = _plugin().media_status(_connection(), {
            "social_set_id": social_set_id, "media_id": media_id})
    except ValueError as exc:
        return _error(exc)
    audit.emit("typefully", "typefully.media_status", subject, outcome="ok")
    return _json_response(200, payload)


def route_typefully(event, method, path):
    """Return a response for Typefully agent routes, or None if unmatched."""
    if path == "/api/agent/typefully/social-sets" and method == "GET":
        return social_sets_api(event)
    if path == "/api/agent/typefully/drafts" and method == "GET":
        return drafts_api(event)
    match = re.fullmatch(r"/api/agent/typefully/social-sets/(\d+)/drafts/(\d+)", path)
    if match and method == "GET":
        return draft_api(event, match.group(1), match.group(2))
    if match and method == "PATCH":
        return patch_draft_api(event, match.group(1), match.group(2))
    match = re.fullmatch(r"/api/agent/typefully/social-sets/(\d+)/media", path)
    if match and method == "POST":
        return upload_media_api(event, match.group(1))
    match = re.fullmatch(r"/api/agent/typefully/social-sets/(\d+)/media/([^/]+)", path)
    if match and method == "GET":
        return media_status_api(event, match.group(1), match.group(2))
    match = re.fullmatch(r"/api/agent/typefully/social-sets/(\d+)/queue", path)
    if match and method == "GET":
        return queue_api(event, match.group(1))
    match = re.fullmatch(r"/api/agent/typefully/social-sets/(\d+)/drafts", path)
    if match and method == "POST":
        return create_draft_api(event, match.group(1))
    return None
