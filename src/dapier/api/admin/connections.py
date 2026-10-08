"""Connection and grant endpoints: OAuth clients, stored
credentials, the save/list flows, grants, and the maintenance ops
(import, token revoke, delete, token issue, discovery, test)."""
from ... import audit as audit_log
from ...auth import authz
import boto3
from ...connections import records as connection_model
from ...connections import credentials
from .. import discovery as discovery_api
from ... import http
from ...connections import importing
import json
from ...connections.providers import oauth_clients
import os
from .. import overview
from ...auth import session
from ...connections import zoom


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

def _save_special_connection(fields, body, operator, connections_table):
    # The provider-specific save paths: Zoom's signing-secret flow (audited
    # here) and the token providers' reuse-stored-token import (which audits
    # through the callback it is handed). None when the generic build-item
    # path handles the provider.
    if fields["provider"] == "zoom":
        status, payload = zoom.save(body, operator_subject=operator,
                                    connections_table=connections_table)
        session._audit_event(fields["connection_id"], audit_log.CONNECT,
                             operator or "unknown",
                             outcome="ok" if status == 200 else "error")
        return status, payload
    if fields["provider"] in connection_model.TOKEN_PROVIDERS:
        return importing.save_token_connection(
            body, operator_subject=operator, connections_table=connections_table,
            audit_event=session._audit_event, action=audit_log.CONNECT,
            reuse_stored_token=True)
    return None


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

    special = _save_special_connection(fields, body, operator, connections_table)
    if special is not None:
        return http._json_response(*special)

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

def list_connections(event):
    """The paged connections list (records.api_list_connections): every
    connection, not just the overview snapshot's first scan page, with
    ``limit``/``next`` paging behind the console's Load more and
    `dapier connections list --all`. Rows carry the same public metadata
    plus token health the overview's connections block renders, and the
    ``used_in`` map saying which workflows and hook triggers reference
    each one."""
    query = event.get("queryStringParameters") or {}
    connections_table = boto3.resource("dynamodb").Table(os.environ["CONNECTIONS_TABLE"])
    status, payload = connection_model.api_list_connections(
        connections_table, limit=query.get("limit"),
        next_token=query.get("next") or None,
    )
    if status == 200:
        payload["connections"] = overview._connection_views(payload["connections"])
        from ...triggers import connection_usage
        connection_usage.attach(payload["connections"])
    return http._json_response(status, payload)

def list_grants(event):
    query = event.get("queryStringParameters") or {}
    status, payload = authz.api_list_grants(
        authz.grants_table(), connection_id=query.get("connection_id") or None,
        limit=query.get("limit"), next_token=query.get("next") or None,
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



def _connection(connection_id):
    return connection_model.get_connection(
        boto3.resource("dynamodb").Table(os.environ["CONNECTIONS_TABLE"]),
        connection_id,
    )

def revoke_connection_tokens(connection_id, operator):
    from ...connections import tokens as token_lifecycle

    connection = _connection(connection_id)
    if not connection:
        return http._json_response(404, {"error": "Connection not found"})
    updated = token_lifecycle.revoke_connection(connection)
    boto3.resource("dynamodb").Table(os.environ["CONNECTIONS_TABLE"]).put_item(Item=updated)
    session._audit_event(connection_id, audit_log.REVOKE, operator, outcome="ok")
    return http._json_response(200, {"connection_id": connection_id, "status": updated["status"]})

def delete_connection(event, connection_id, operator):
    """DELETE /api/admin/connections/{id}: remove the connection, its stored
    credential, and its grants outright — the console mirror of the CLI's
    `dapier connections delete`. A 409 names the workflows and hook triggers
    still referencing it; ``?force=1`` accepts breaking those."""
    query = event.get("queryStringParameters") or {}
    status, payload = connection_model.api_delete_connection(
        boto3.resource("dynamodb").Table(os.environ["CONNECTIONS_TABLE"]),
        connection_id,
        grants_table_ref=authz.grants_table(),
        force=query.get("force") in ("1", "true", "yes"),
    )
    if status == 200:
        session._audit_event(connection_id, "connections.delete", operator, outcome="ok")
    return http._json_response(status, payload)

def issue_connection_token(connection_id, operator):
    """Console mirror of the CLI's fresh provider access token (same domain call).

    `dapier token exec|write` mint short-lived provider tokens through
    /api/agent/token; this gives the Connections view the same outcome
    without the CLI. The value is only ever returned to an operator session
    and never persisted.
    """
    from ...connections import tokens as token_lifecycle
    from ...connections.records import BindingError
    from ...connections.tokens import TokenError

    connection = _connection(connection_id)
    if not connection:
        return http._json_response(404, {"error": "Connection not found"})
    try:
        access_token, info = token_lifecycle.get_access_token(connection)
    except BindingError as exc:
        session._audit_event(connection_id, "connections.token", operator or "unknown",
                     outcome="error", error=str(exc))
        return http._json_response(409, {"error": str(exc)})
    except TokenError:
        session._audit_event(connection_id, "connections.token", operator or "unknown",
                     outcome="error", error="provider-token-unavailable")
        return http._json_response(502, {"error": "Provider token is unavailable"})
    session._audit_event(connection_id, "connections.token", operator or "unknown", outcome="ok")
    response = http._json_response(200, {
        "connection_id": connection_id,
        "provider": connection["provider"],
        "access_token": access_token,
        "expires_at": info["expires_at"],
        "scope": info["scope"],
        "provider_account_id": info["provider_account_id"],
        "account_title": info.get("account_title"),
        "refreshed": info["refreshed"],
    })
    response["headers"]["cache-control"] = "no-store"
    return response

def discover_connection(connection_id, event, resource=None):
    """Console mirror of the CLI discovery endpoints (same domain module).

    Without a resource: the provider's discoverable-resource catalog; with
    one: that resource's live items, fetched with the connection's token.
    The domain layer is api.discovery, shared verbatim with the agent API;
    resource metadata comes from the connector registry. Unknown connections
    — including the aws/s3 pseudo-connections — are the domain's 404.
    """
    if resource is None:
        status, payload = discovery_api.resources(connection_id)
        return http._json_response(status, payload)
    query = event.get("queryStringParameters") or {}
    status, payload = discovery_api.discover(connection_id, resource, query)
    return http._json_response(status, payload)

def test_connection(connection_id, event, operator):
    """Console mirror of the connection health test (same domain module)."""
    status, payload = discovery_api.test_connection(connection_id)
    session._audit_event(connection_id, "connections.test", operator or "unknown",
                 outcome="ok" if payload.get("ok") else "error",
                 error=None if payload.get("ok") else str(payload.get("detail")))
    return http._json_response(status, payload)


