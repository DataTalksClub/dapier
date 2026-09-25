"""Operator console endpoints: thin JSON wrappers over the domain modules."""
import json
import os

import boto3

from ... import audit as audit_log
from ... import http
from ...auth import api_tokens, authz, session
from ...connections import credentials, importing
from ...connections import records as connection_model
from ...connections.providers import oauth_clients, slack_tokens, telegram_api
from ...triggers import email_triggers, hook_triggers, schedule_triggers
from .. import designer_store, overview, runs


def list_runs(event):
    """Recent runs, one row per workflow handling of a trigger event."""
    query = event.get("queryStringParameters") or {}
    status, payload = runs.api_list(query.get("limit", 25))
    return http._json_response(status, payload)


def get_run(run_id):
    """One run's step-by-step flow: status, input, output, duration, error."""
    status, payload = runs.api_get(run_id)
    return http._json_response(status, payload)


def oauth_clients_view():
    return http._json_response(200, {
        "clients": [overview._oauth_client_status(provider) for provider in oauth_clients.CANONICAL_PROVIDERS],
    })

def save_oauth_client(provider, event):
    """Store the shared OAuth client for a provider in the config DB.

    Runtime-reconfigurable: no redeploy needed. The secret is write-only —
    the response reports presence, not the value.
    """
    try:
        body = http._request_json(event)
    except (ValueError, json.JSONDecodeError):
        return http._json_response(400, {"error": "Invalid request"})
    status, payload = oauth_clients.api_save_client(
        provider, body.get("client_id"), body.get("client_secret"))
    if status == 200:
        session._audit_event(f"oauth-client#{payload['provider']}", audit_log.CONFIG,
                     session._session_subject(event) or "unknown", outcome="ok")
    return http._json_response(status, payload)

def save_credential(provider, event):
    try:
        body = http._request_json(event)
    except (ValueError, json.JSONDecodeError):
        return http._json_response(400, {"error": "Invalid request"})
    status, payload = credentials.api_save_credential(provider, body)
    return http._json_response(status, payload)

def save_connection(event):
    try:
        body = http._request_json(event)
    except (ValueError, json.JSONDecodeError):
        return http._json_response(400, {"error": "Invalid request"})
    try:
        fields = connection_model.validate_new_connection(body)
    except connection_model.ConnectionError as exc:
        return http._json_response(400, {"error": str(exc)})

    connections_table = boto3.resource("dynamodb").Table(os.environ["CONNECTIONS_TABLE"])
    previous = connection_model.get_connection(connections_table, fields["connection_id"])
    operator = session._session_subject(event)

    if fields["provider"] in connection_model.TOKEN_PROVIDERS:
        return _save_token_connection(fields, body, previous, operator, connections_table)

    try:
        item = connection_model.build_item(
            fields, owner_subject=operator, previous=previous,
        )
    except connection_model.BindingError as exc:
        session._audit_event(fields["connection_id"], audit_log.CONNECT, operator or "unknown",
                     outcome="error", error=str(exc))
        return http._json_response(409, {"error": str(exc)})

    connection_model.put_connection(connections_table, item)
    session._audit_event(item["connection_id"], audit_log.CONNECT, operator or "unknown", outcome="ok")
    return http._json_response(200, item)

def _save_token_connection(fields, body, previous, operator, connections_table):
    """Create/update a connection that authenticates with a pasted token.

    The token is verified against the provider before anything is stored;
    an edit without a new token re-verifies and keeps the stored one.
    """
    try:
        token = body.get("token") or credentials.get_credential(
            connection_model.credential_id_for(fields["connection_id"]),
        ).get("token")
    except KeyError:
        token = None
    if not token:
        hint = ("A Slack bot (xoxb-) or user (xoxp-) token is required"
                if fields["provider"] == "slack"
                else "A Telegram bot token from @BotFather is required")
        return http._json_response(400, {"error": hint})
    try:
        account_id, account_title = importing.verify_token_provider(fields["provider"], token)
    except (slack_tokens.SlackTokenError, telegram_api.TelegramApiError) as exc:
        session._audit_event(fields["connection_id"], audit_log.CONNECT, operator or "unknown",
                     outcome="error", error=str(exc))
        return http._json_response(400, {"error": str(exc)})
    try:
        item = connection_model.build_item(fields, owner_subject=operator, previous=previous)
        connection_model.check_binding(item, account_id)
        item = connection_model.mark_connected(
            item, verified_account_id=account_id, account_title=account_title,
            granted_scopes=fields["scopes"], connected_by=operator,
        )
    except connection_model.BindingError as exc:
        session._audit_event(fields["connection_id"], audit_log.CONNECT, operator or "unknown",
                     outcome="denied-account-mismatch", error=str(exc))
        return http._json_response(409, {"error": str(exc)})
    except connection_model.ConnectionError as exc:
        session._audit_event(fields["connection_id"], audit_log.CONNECT, operator or "unknown",
                     outcome="error", error=str(exc))
        return http._json_response(400, {"error": str(exc)})
    credentials.put_credential(item["credential_id"], {"token": token}, provider=fields["provider"])
    connection_model.put_connection(connections_table, item)
    session._audit_event(item["connection_id"], audit_log.CONNECT, operator or "unknown", outcome="ok")
    return http._json_response(200, connection_model.public_view(item))

def list_grants(event):
    query = event.get("queryStringParameters") or {}
    status, payload = authz.api_list_grants(
        authz.grants_table(), connection_id=query.get("connection_id") or None,
    )
    return http._json_response(status, payload)

def save_grant(event, operator):
    try:
        body = http._request_json(event)
    except (ValueError, json.JSONDecodeError):
        return http._json_response(400, {"error": "Invalid request"})
    status, payload = authz.api_save_grant(
        authz.grants_table(), body, operator=operator,
        connections_table=boto3.resource("dynamodb").Table(os.environ["CONNECTIONS_TABLE"]),
    )
    if status == 200:
        session._audit_event(payload["connection_id"], audit_log.GRANT, operator,
                     agent=payload["agent"], outcome="ok")
    return http._json_response(status, payload)

def delete_grant(event, operator):
    query = event.get("queryStringParameters") or {}
    status, payload = authz.api_delete_grant(
        authz.grants_table(), query.get("connection_id"), query.get("grantee"),
    )
    if status == 200:
        session._audit_event(str(query.get("connection_id", "")).strip().lower(),
                     audit_log.GRANT, operator, outcome="revoked")
    return http._json_response(status, payload)

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

def designer_list(event):
    status, payload = designer_store.api_list()
    return http._json_response(status, payload)

def designer_get(source):
    status, payload = designer_store.api_get(source)
    return http._json_response(status, payload)

def save_designer_workflow(event, operator):
    try:
        body = http._request_json(event)
        status, payload = designer_store.api_save(body, operator=operator)
    except (ValueError, json.JSONDecodeError) as exc:
        return http._json_response(400, {"error": str(exc) or "Invalid request"})
    session._audit_event(str(payload.get("file", "unknown")), "workflow.save", operator,
                 outcome="ok" if status == 200 else "error", error=payload.get("error"))
    return http._json_response(status, payload)

def toggle_designer_workflow(event, operator, source):
    try:
        body = http._request_json(event)
        status, payload = designer_store.api_toggle(source, body, operator=operator)
    except (ValueError, json.JSONDecodeError) as exc:
        return http._json_response(400, {"error": str(exc) or "Invalid request"})
    session._audit_event(str(source), "workflow.toggle", operator,
                 outcome="ok" if status == 200 else "error", error=payload.get("error"))
    return http._json_response(status, payload)

def _hook_kind(event, body=None):
    """The hook trigger kind, from the query string or the request body."""
    query = event.get("queryStringParameters") or {}
    kind = (body or {}).get("kind") or query.get("kind") or "webhook"
    kind = str(kind).strip().lower()
    if kind not in hook_triggers.KINDS:
        raise email_triggers.TriggerError(
            f"hook kind must be one of: {', '.join(hook_triggers.KINDS)}")
    return kind

def list_hook_triggers(event):
    try:
        kind = _hook_kind(event)
    except email_triggers.TriggerError as exc:
        return http._json_response(400, {"error": str(exc)})
    status, payload = hook_triggers.api_list(kind=kind)
    return http._json_response(status, payload)

def save_hook_trigger(event, operator):
    try:
        body = http._request_json(event)
        kind = _hook_kind(event, body)
        status, payload = hook_triggers.api_save(body, operator, kind)
    except (ValueError, json.JSONDecodeError) as exc:
        return http._json_response(400, {"error": str(exc) or "Invalid request"})
    session._audit_event(payload.get("hook_id", "unknown"), "hook-trigger.save", operator,
                 outcome="created" if payload.get("created") else "updated")
    return http._json_response(status, payload)

def delete_hook_trigger(event, operator):
    try:
        kind = _hook_kind(event)
        query = event.get("queryStringParameters") or {}
        status, payload = hook_triggers.api_delete(
            query.get("name", ""), operator, kind=kind)
    except email_triggers.TriggerError as exc:
        return http._json_response(404, {"error": str(exc)})
    session._audit_event(payload.get("hook_id", "unknown"), "hook-trigger.delete", operator, outcome="deleted")
    return http._json_response(status, payload)

def list_schedule_triggers(event):
    status, payload = schedule_triggers.api_list()
    return http._json_response(status, payload)

def save_schedule_trigger(event, operator):
    try:
        body = http._request_json(event)
        status, payload = schedule_triggers.api_save(body, operator)
    except (ValueError, json.JSONDecodeError) as exc:
        return http._json_response(400, {"error": str(exc) or "Invalid request"})
    session._audit_event(payload.get("schedule_id", "unknown"), "schedule-trigger.save", operator,
                 outcome="created" if payload.get("created") else "updated")
    return http._json_response(status, payload)

def delete_schedule_trigger(event, operator):
    try:
        query = event.get("queryStringParameters") or {}
        status, payload = schedule_triggers.api_delete(query.get("name", ""), operator)
    except email_triggers.TriggerError as exc:
        return http._json_response(404, {"error": str(exc)})
    session._audit_event(payload.get("schedule_id", "unknown"), "schedule-trigger.delete",
                 operator, outcome="deleted")
    return http._json_response(status, payload)

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

def _connection(connection_id):
    return connection_model.get_connection(
        boto3.resource("dynamodb").Table(os.environ["CONNECTIONS_TABLE"]),
        connection_id,
    )

def import_connection(event, operator):
    """One-time operator import of an existing provider credential (cookie path)."""
    try:
        body = http._request_json(event)
    except (ValueError, json.JSONDecodeError):
        return http._json_response(400, {"error": "Invalid request"})
    connections_table = boto3.resource("dynamodb").Table(os.environ["CONNECTIONS_TABLE"])
    status, payload = importing.import_core(body, operator_subject=operator, connections_table=connections_table)
    return http._json_response(status, payload)

def revoke_connection_tokens(connection_id, operator):
    from ...connections import tokens as token_lifecycle

    connection = _connection(connection_id)
    if not connection:
        return http._json_response(404, {"error": "Connection not found"})
    updated = token_lifecycle.revoke_connection(connection)
    boto3.resource("dynamodb").Table(os.environ["CONNECTIONS_TABLE"]).put_item(Item=updated)
    session._audit_event(connection_id, audit_log.REVOKE, operator, outcome="ok")
    return http._json_response(200, {"connection_id": connection_id, "status": updated["status"]})
