"""Connection lifecycle: grant-filtered listing, metadata and
scope edits, provider-token issuance, consent, discovery,
health tests, and the operator import/revoke/delete paths."""

import hashlib
import json
import os
import time

from .. import discovery as discovery_api
from ...auth import authz, session
from ...connections import importing
from ...connections import records as connections
from ...connections import tokens
from ...connections.providers import oauth_clients, oauth_providers
from ...connections.providers.oauth_providers import connection_grant_scopes
from ...connections.records import BindingError
from ...connections.tokens import TokenError
from ...connections import oauth_flow
from .. import overview

from ... import connection_digest

from .common import _json_response, _no_store, _check_rate, _api_token, _check_agent_binding, _b64encode

from .common import _LateBinding

# Shared dependencies resolved through the agent package at call time:
# tests patch agent.<name> and every route module must see the patch.
authenticate = _LateBinding("authenticate")
require_operator = _LateBinding("require_operator")
_is_operator = _LateBinding("_is_operator")
_tables = _LateBinding("_tables")
audit = _LateBinding("audit")
verify_id_token = _LateBinding("verify_id_token")


__all__ = ["AGENT_LIST_DEFAULT_LIMIT", "AGENT_LIST_MAX_LIMIT", "connections_discover_api", "connections_test_api", "create_connection", "delete_connection", "expiry_digest_api", "import_connection", "issue_token", "list_for_caller", "revoke_connection_tokens", "show_connection", "start_connect", "update_connection_metadata"]



def issue_token(event):
    subject, error = authenticate(event)
    if error:
        return error
    try:
        body = json.loads(event.get("body") or "{}")
    except (ValueError, AttributeError):
        return _json_response(400, {"error": "Invalid request"})
    if not isinstance(body, dict):
        return _json_response(400, {"error": "Invalid request"})
    connection_id = str(body.get("connection_id", "")).strip().lower()
    try:
        agent = authz.validate_agent(body.get("agent", ""))
    except ValueError as exc:
        return _json_response(400, {"error": str(exc)})
    if _check_agent_binding(event, agent):
        bound = _api_token(event).get("agent")
        audit.emit(connection_id, audit.TOKEN, subject, agent=agent, outcome="denied-agent-mismatch")
        return _json_response(403, {"error": f"This API token is bound to agent '{bound}'"})
    if not connection_id:
        return _json_response(400, {"error": "Connection ID and agent are required"})
    if not _check_rate(subject, connection_id):
        audit.emit(connection_id, audit.TOKEN, subject, agent=agent, outcome="denied-rate-limited")
        return _json_response(429, {"error": "Token requests are rate-limited; retry shortly"})

    connections_table, grants_table = _tables()
    connection = connections.get_connection(connections_table, connection_id)
    if not connection:
        return _json_response(404, {"error": "Connection not found"})
    if not authz.check_grant(grants_table, subject=subject, agent=agent,
                             connection_id=connection_id, operation="use"):
        audit.emit(connection_id, audit.TOKEN, subject, agent=agent, outcome="denied-no-grant")
        return _json_response(403, {"error": "No grant for this connection and agent"})
    try:
        access_token, info = tokens.get_access_token(connection)
    except BindingError as exc:
        audit.emit(connection_id, audit.TOKEN, subject, agent=agent, outcome="error", error=str(exc))
        return _json_response(409, {"error": str(exc)})
    except TokenError as exc:
        audit.emit(connection_id, audit.TOKEN, subject, agent=agent, outcome="error", error=str(exc))
        return _json_response(502, {"error": "Provider token is unavailable"})
    audit.emit(connection_id, audit.TOKEN, subject, agent=agent, outcome="ok")
    return _no_store(_json_response(200, {
        "connection_id": connection_id,
        "provider": connection["provider"],
        "access_token": access_token,
        "expires_at": info["expires_at"],
        "scope": info["scope"],
        "provider_account_id": info["provider_account_id"],
        "account_title": info.get("account_title"),
        "refreshed": info["refreshed"],
    }))


AGENT_LIST_DEFAULT_LIMIT = 100
AGENT_LIST_MAX_LIMIT = 200


def list_for_caller(event):
    """The caller's connections (grant-filtered), or every connection for an
    operator passing ``?all=true``.

    Default (unchanged): the connections this identity holds grants for, one
    row per grant. ``limit``/``next`` page that list — rows sort by
    (connection_id, agent) and ``paging`` is additive, so the no-param
    response shape is exactly as before. The grant scan is a full walk, so a
    caller's later grants no longer clip at DynamoDB's page limit.

    ``?all=true`` is the operator bulk list (``dapier connections list
    --all``): every connection, paged over records.api_list_connections and
    gated like the other operator-only reads.
    """
    subject, error = authenticate(event)
    if error:
        return error
    query = event.get("queryStringParameters") or {}
    connections_table, grants_table = _tables()
    if str(query.get("all") or "").strip().lower() in ("1", "true", "yes"):
        _, operator_error = require_operator(event, "connections.list")
        if operator_error:
            return operator_error
        status, payload = connections.api_list_connections(
            connections_table, limit=query.get("limit"),
            next_token=query.get("next") or None,
        )
        if status == 200:
            payload["connections"] = overview._connection_views(payload["connections"])
            from ...triggers import connection_usage
            connection_usage.attach(payload["connections"])
        return _no_store(_json_response(status, payload))
    try:
        items = authz.list_grants(grants_table)
    except Exception:
        items = []
    mine = [item for item in items if item.get("subject") == subject and not _grant_expired(item)]
    views = []
    for item in mine:
        connection = connections.get_connection(connections_table, item["connection_id"])
        if not connection:
            continue
        views.append({
            **connections.public_view(connection, tokens.stored_value(item["connection_id"])),
            "agent": item.get("agent"),
            "operations": item.get("operations", []),
        })
    views.sort(key=lambda view: (view["connection_id"], view.get("agent") or ""))
    if query.get("limit") is None and not query.get("next"):
        return _json_response(200, {"connections": views})
    limit = max(1, min(
        _int(query.get("limit")) or AGENT_LIST_DEFAULT_LIMIT, AGENT_LIST_MAX_LIMIT))
    token_key = authz.decode_paging_token(query.get("next")) if query.get("next") else None
    if query.get("next") and token_key is None:
        return _json_response(400, {"error": "Invalid page token"})
    def view_key(view):
        return (view["connection_id"], view.get("agent") or "")
    keyed = [(view_key(view), view) for view in views]
    if token_key is not None:
        keyed = [entry for entry in keyed if entry[0] > token_key]
    page = [view for _, view in keyed[:limit]]
    more = len(keyed) > limit
    return _json_response(200, {
        "connections": page,
        "paging": {
            "next": authz.encode_paging_token(keyed[limit - 1][0]) if more and page else None,
            "limit": limit,
        },
    })


def expiry_digest_api(event):
    """Operator-only send-now for the daily connection-expiry digest.

    CLI fire-now (`dapier connections send-expiry-digest`). Same domain
    function the scheduled ConnectionDigestFunction Lambda runs
    (connection_digest.send); the response reports what was sent, or
    ``skipped`` when nothing expires in the window — no noise email.
    """
    subject, error = require_operator(event, "connections.send-expiry-digest")
    if error:
        return error
    payload = connection_digest.send()
    if payload.get("sent"):
        audit.emit("connections", "connections.send-expiry-digest",
                   subject, outcome="ok")
    return _no_store(_json_response(200, payload))


def _int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _grant_expired(item):
    expires_at = item.get("expires_at")
    if not expires_at:
        return False
    try:
        return int(expires_at) <= int(time.time())
    except (TypeError, ValueError):
        return True


def show_connection(event, connection_id):
    subject, error = authenticate(event)
    if error:
        return error
    query = event.get("queryStringParameters") or {}
    agent = str(query.get("agent", "") or "").strip().lower() or None
    connections_table, grants_table = _tables()
    connection = connections.get_connection(connections_table, connection_id)
    if not connection:
        return _json_response(404, {"error": "Connection not found"})
    stored = tokens.stored_value(connection_id)
    if _is_operator(event, subject):
        return _json_response(200, connections.public_view(connection, stored))
    if agent:
        try:
            authz.validate_agent(agent)
        except ValueError as exc:
            return _json_response(400, {"error": str(exc)})
        pairs = [(subject, agent)]
    else:
        try:
            items = authz.list_grants(grants_table, connection_id=connection_id)
        except Exception:
            items = []
        pairs = [
            (item.get("subject"), item.get("agent"))
            for item in items
            if item.get("connection_id") == connection_id and not _grant_expired(item)
        ]
    allowed = any(
        authz.check_grant(grants_table, subject=pair_subject, agent=pair_agent,
                          connection_id=connection_id, operation="use")
        for pair_subject, pair_agent in pairs
        if pair_subject == subject
    )
    if not allowed:
        return _json_response(404, {"error": "Connection not found"})
    return _json_response(200, connections.public_view(connection, stored))


def create_connection(event):
    """Operator-only provision of an OAuth connection before consent."""
    subject, error = require_operator(event, audit.CONNECT)
    if error:
        return error
    try:
        body = json.loads(event.get("body") or "{}")
    except (ValueError, AttributeError, json.JSONDecodeError):
        return _json_response(400, {"error": "Invalid request"})
    allowed = {"connection_id", "provider", "display_name", "scopes", "root_path"}
    if not isinstance(body, dict) or not {"connection_id", "provider"} <= set(body) or set(body) - allowed:
        return _json_response(400, {"error": "Provide connection_id, provider, and supported connection fields"})
    try:
        fields = connections.validate_new_connection(body)
    except connections.ConnectionError as exc:
        return _json_response(400, {"error": str(exc)})
    if fields["provider"] in connections.TOKEN_PROVIDERS:
        return _json_response(400, {"error": "Token providers must be created with connections import and a verified token"})

    connections_table, _ = _tables()
    if connections.get_connection(connections_table, fields["connection_id"]):
        return _json_response(409, {"error": "Connection already exists"})
    item = connections.build_item(fields, owner_subject=subject)
    connections.put_connection(connections_table, item)
    audit.emit(item["connection_id"], audit.CONNECT, subject, outcome="created")
    return _json_response(200, connections.public_view(item))


def update_connection_metadata(event, connection_id):
    """Operator-only edit of a connection's display name, scopes, or Dropbox path."""
    subject, error = require_operator(event, audit.CONNECT)
    if error:
        return error
    try:
        body = json.loads(event.get("body") or "{}")
    except (ValueError, AttributeError, json.JSONDecodeError):
        return _json_response(400, {"error": "Invalid request"})
    allowed = {"display_name", "scopes", "root_path"}
    if not isinstance(body, dict) or not body or set(body) - allowed:
        return _json_response(400, {"error": "Provide supported connection fields"})
    if "scopes" in body and (
        not isinstance(body["scopes"], list)
        or not all(isinstance(scope, str) for scope in body["scopes"])
    ):
        return _json_response(400, {"error": "Scopes must be a list of strings"})

    connections_table, _ = _tables()
    previous = connections.get_connection(connections_table, connection_id)
    if not previous:
        return _json_response(404, {"error": "Connection not found"})
    if previous.get("provider") in connections.TOKEN_PROVIDERS and "scopes" in body:
        return _json_response(400, {"error": "This provider carries its scopes in the supplied token"})
    try:
        fields = connections.validate_new_connection({
            "connection_id": connection_id,
            "provider": previous.get("provider"),
            "display_name": body.get("display_name", previous.get("display_name") or connection_id),
            "scopes": body.get("scopes", previous.get("scopes") or []),
            "expected_account_id": previous.get("expected_account_id"),
            "root_path": body.get("root_path", previous.get("root_path") or ""),
        })
        item = connections.build_item(fields, owner_subject=subject, previous=previous)
    except connections.BindingError as exc:
        return _json_response(409, {"error": str(exc)})
    except connections.ConnectionError as exc:
        return _json_response(400, {"error": str(exc)})

    connections.put_connection(connections_table, item)
    audit.emit(connection_id, audit.CONNECT, subject, outcome="ok")
    return _json_response(200, connections.public_view(item))


def revoke_connection_tokens(event, connection_id):
    """Operator-only token revoke over the CLI's bearer authentication."""
    subject, error = require_operator(event, audit.REVOKE)
    if error:
        return error
    connections_table, _ = _tables()
    connection = connections.get_connection(connections_table, connection_id)
    if not connection:
        return _json_response(404, {"error": "Connection not found"})
    updated = tokens.revoke_connection(connection)
    connections.put_connection(connections_table, updated)
    audit.emit(connection_id, audit.REVOKE, subject, outcome="ok")
    return _json_response(200, {"connection_id": connection_id, "status": updated["status"]})


def delete_connection(event, connection_id):
    """Operator-only connection delete: the record, its stored credential,
    and its grants go together. A 409 names the workflows and hook triggers
    still referencing the connection (the same usage map the connections
    list displays); ``?force=1`` deletes anyway."""
    subject, error = require_operator(event, "connections.delete")
    if error:
        return error
    connections_table, grants_table = _tables()
    query = event.get("queryStringParameters") or {}
    status, payload = connections.api_delete_connection(
        connections_table, connection_id,
        grants_table_ref=grants_table,
        force=query.get("force") in ("1", "true", "yes"),
    )
    if status == 200:
        audit.emit(connection_id, "connections.delete", subject, outcome="ok")
    return _no_store(_json_response(status, payload))


def _operator_connection(event, connection_id, action):
    """Authenticate an operator and load the connection they named.

    Returns ``(subject, connection, None)`` or ``(None, None, error_response)``.
    Discovery reads, like every other management action, are operator-gated.
    """
    subject, error = require_operator(event, action)
    if error:
        return None, None, error
    connections_table, _ = _tables()
    connection = connections.get_connection(connections_table, connection_id)
    if not connection:
        return None, None, _json_response(404, {"error": f"Unknown connection '{connection_id}'"})
    return subject, connection, None


def connections_discover_api(event, connection_id, resource=None):
    """Operator-only Zapier-style discovery over the CLI's bearer authentication.

    Without a resource: the connection provider's discoverable-resource
    catalog. With one: the live items of that resource, fetched with the
    connection's own token (query string carries the resource's params).
    The domain layer is api.discovery, shared verbatim with the console;
    resource metadata comes from the connector registry. Unknown connections
    — including the aws/s3 pseudo-connections — are the domain's 404, so the
    operator gate here is require_operator only.
    """
    subject, error = require_operator(event, "connections.discover")
    if error:
        return error
    connections_table, _ = _tables()
    if resource is None:
        status, payload = discovery_api.resources(
            connection_id, connections_table=connections_table)
        return _json_response(status, payload)
    query = event.get("queryStringParameters") or {}
    status, payload = discovery_api.discover(
        connection_id, resource, query, connections_table=connections_table)
    return _json_response(status, payload)


def connections_test_api(event, connection_id):
    """Operator-only connection health test (mirrors the console's test)."""
    subject, error = require_operator(event, "connections.test")
    if error:
        return error
    connections_table, _ = _tables()
    status, payload = discovery_api.test_connection(
        connection_id, connections_table=connections_table)
    audit.emit(connection_id, "connections.test", subject,
               outcome="ok" if payload.get("ok") else "error",
               error=None if payload.get("ok") else str(payload.get("detail")))
    return _json_response(status, payload)


def start_connect(event, connection_id):
    """Initiate a CLI-bound provider consent. Returns the authorize URL.

    Allowed for operators (by stable subject or email claim) or callers with
    a ``connect`` grant for the requested agent. The signed state binds the
    operator subject and PKCE verifier, so the public callback can complete
    the flow without a browser session cookie.
    """
    subject, error = authenticate(event)
    if error:
        return error
    try:
        body = json.loads(event.get("body") or "{}")
    except (ValueError, AttributeError):
        return _json_response(400, {"error": "Invalid request"})
    if not isinstance(body, dict):
        return _json_response(400, {"error": "Invalid request"})
    try:
        agent = authz.validate_agent(body.get("agent", ""))
    except ValueError as exc:
        return _json_response(400, {"error": str(exc)})
    if _check_agent_binding(event, agent):
        bound = _api_token(event).get("agent")
        audit.emit(connection_id, audit.CALLBACK, subject, agent=agent,
                   outcome="denied-agent-mismatch")
        return _json_response(403, {"error": f"This API token is bound to agent '{bound}'"})

    connections_table, grants_table = _tables()
    connection = connections.get_connection(connections_table, connection_id)
    if not connection:
        return _json_response(404, {"error": "Connection not found"})
    allowed = (
        _is_operator(event, subject)
        or authz.check_grant(grants_table, subject=subject, agent=agent,
                             connection_id=connection_id, operation="connect")
    )
    if not allowed:
        audit.emit(connection_id, audit.CALLBACK, subject, agent=agent,
                   outcome="denied-not-authorized")
        return _json_response(403, {"error": "Not authorized to connect this connection"})
    redirect_uri = oauth_flow.oauth_callback_url()
    if connection["provider"] in connections.TOKEN_PROVIDERS:
        return _json_response(
            400,
            {"error": "This provider connects with a directly provided token; "
                      "an operator can paste a new one in the operator console"},
        )
    if not redirect_uri:
        return _json_response(503, {"error": "OAuth callback URL is not configured"})
    try:
        scopes = connection_grant_scopes(connection["provider"], connection.get("scopes"))
    except oauth_providers.ProviderError as exc:
        return _json_response(400, {"error": str(exc)})
    verifier = _b64encode(os.urandom(48))
    challenge = _b64encode(hashlib.sha256(verifier.encode()).digest())
    state = session._sign({
        "kind": "oauth",
        "connection_id": connection_id,
        "redirect_uri": redirect_uri,
        "code_verifier": verifier,
        "jti": _b64encode(os.urandom(16)),
        "operator_subject": subject,
        "exp": int(time.time()) + 600,
    })
    try:
        client_id, _ = oauth_clients.get(connection["provider"])
    except oauth_clients.ClientConfigError as exc:
        return _json_response(503, {"error": str(exc)})
    audit.emit(connection_id, audit.CALLBACK, subject, agent=agent, outcome="connect-started")
    return _json_response(200, {
        "connection_id": connection_id,
        "authorize_url": oauth_providers.authorization_url(
            connection["provider"],
            client_id=client_id,
            redirect_uri=redirect_uri,
            scopes=scopes,
            state=state,
            code_challenge=challenge,
        ),
        "expires_in": 600,
    })


def import_connection(event):
    subject, error = authenticate(event)
    if error:
        return error
    if not _is_operator(event, subject):
        audit.emit("unknown", audit.IMPORT, subject, outcome="denied-not-operator")
        return _json_response(403, {"error": "Operator authorization required"})
    try:
        body = json.loads(event.get("body") or "{}")
    except (ValueError, AttributeError):
        return _json_response(400, {"error": "Invalid request"})
    if not isinstance(body, dict):
        return _json_response(400, {"error": "Invalid request"})
    connections_table, _ = _tables()
    status, payload = importing.import_core(body, operator_subject=subject, connections_table=connections_table)
    return _json_response(status, payload)
