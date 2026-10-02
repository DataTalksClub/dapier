"""The email inventory endpoint and the sender allowlist."""
from ...triggers import email_from
from ... import http
import json
from ...auth import session


def list_email_triggers(event):
    from ...triggers.email_routes import inventory
    return http._json_response(200, inventory())


def email_from_list(event):
    try:
        status, payload = email_from.api_list()
    except email_from.FromError as exc:
        return http._json_response(400, {"error": str(exc)})
    return http._json_response(status, payload)


def email_from_add(event, operator):
    try:
        body = http._request_json(event)
        status, payload = email_from.api_add((body or {}).get("address"))
    except (email_from.FromError, ValueError, json.JSONDecodeError) as exc:
        return http._json_response(400, {"error": str(exc) or "Invalid request"})
    session._audit_event("email-from", "email-from", operator, outcome="added")
    return http._json_response(status, payload)


def email_from_remove(event, operator):
    query = event.get("queryStringParameters") or {}
    try:
        status, payload = email_from.api_remove(query.get("address", ""))
    except email_from.FromError as exc:
        return http._json_response(400, {"error": str(exc)})
    session._audit_event("email-from", "email-from", operator, outcome="removed")
    return http._json_response(status, payload)
