"""Email trigger endpoints and the sender allowlist."""
from ...triggers import email_from
from ...triggers import email_triggers
from ... import http
import json
from ...auth import session


def list_email_triggers(event):
    from ...triggers.email_routes import inventory
    return http._json_response(200, inventory())


def save_email_trigger(event, operator):
    return http._json_response(410, {"error": "Email flows are defined only in Workflows. Save and publish a workflow with an email trigger."})


def delete_email_trigger(event, operator):
    return http._json_response(410, {"error": "Edit or delete the owning workflow. Email addresses have no separate flow definition."})


def migrate_email_trigger(event, operator):
    from ...triggers.email_routes import migrate
    try:
        body = http._request_json(event)
        if not isinstance(body, dict):
            raise ValueError("request body must be an object")
        status, payload = migrate(body.get("name"), operator)
    except ValueError as exc:
        return http._json_response(400, {"error": str(exc)})
    session._audit_event(str(body.get("name") or "unknown"), "email-trigger.migrate", operator,
                         outcome="migrated" if status == 200 else "error")
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


