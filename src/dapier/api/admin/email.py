"""Email trigger endpoints and the sender allowlist."""
from ...triggers import email_from
from ...triggers import email_triggers
from ... import http
import json
from ...auth import session


def list_email_triggers(event):
    status, payload = email_triggers.api_list()
    return http._json_response(status, payload)

def save_email_trigger(event, operator):
    try:
        body = http._request_json(event)
        status, payload = email_triggers.api_save(body, operator)
    except (ValueError, json.JSONDecodeError) as exc:
        return http._json_response(400, {"error": str(exc) or "Invalid request"})
    session._audit_event(payload.get("name", "unknown"), "email-trigger.save", operator,
                 outcome="created" if payload.get("created") else "updated")
    return http._json_response(status, payload)

def delete_email_trigger(event, operator):
    query = event.get("queryStringParameters") or {}
    try:
        status, payload = email_triggers.api_delete(query.get("name", ""), operator)
    except email_triggers.TriggerError as exc:
        return http._json_response(404, {"error": str(exc)})
    session._audit_event(payload.get("name", "unknown"), "email-trigger.delete", operator, outcome="deleted")
    return http._json_response(status, payload)


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


