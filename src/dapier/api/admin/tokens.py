"""API token endpoints: list, create, revoke, delete."""
from ...auth import api_tokens
from ... import audit as audit_log
from ...auth import authz
from ... import http
import json
from ...auth import session


def list_api_tokens(event):
    status, payload = api_tokens.api_list()
    return http._json_response(status, payload)

def create_api_token(event, operator):
    try:
        body = http._request_json(event)
        status, payload = api_tokens.api_create(body, operator)
    except (ValueError, json.JSONDecodeError) as exc:
        return http._json_response(400, {"error": str(exc) or "Invalid request"})
    if status == 200:
        session._audit_event(f"api-token#{payload['token_id']}", audit_log.API_TOKEN,
                     operator, agent=payload["agent"], outcome="created")
    return http._json_response(status, payload)

def revoke_api_token(event, operator):
    query = event.get("queryStringParameters") or {}
    status, payload = api_tokens.api_revoke(query.get("token_id"))
    if status == 200:
        session._audit_event(f"api-token#{payload.get('token_id', 'unknown')}",
                     audit_log.API_TOKEN, operator, outcome="revoked")
    return http._json_response(status, payload)

def delete_api_token(event, operator):
    """DELETE /api/admin/tokens: revoke by default; purge=1 permanently removes
    an already-revoked token and deletes its grants (console mirror of the
    CLI's `dapier tokens delete`)."""
    query = event.get("queryStringParameters") or {}
    if query.get("purge") not in ("1", "true", "yes"):
        return revoke_api_token(event, operator)
    status, payload = api_tokens.api_delete(
        query.get("token_id"), grants_table_ref=authz.grants_table())
    if status == 200:
        session._audit_event(f"api-token#{payload['token_id']}",
                     audit_log.API_TOKEN, operator, outcome="deleted")
    return http._json_response(status, payload)

