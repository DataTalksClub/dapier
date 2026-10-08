"""Connection discovery and health checks, shared by the console and CLI.

Both API surfaces expose the same three behaviors over a connection:

- ``GET …/connections/{cid}/discover`` lists the discovery resources the
  connection's provider offers (``resources``);
- ``GET …/connections/{cid}/discover/{resource}`` runs one resource and
  returns its ``items`` (query string supplies the params);
- ``POST …/connections/{cid}/test`` exercises the stored credentials against
  the provider and always answers HTTP 200 with ``{"ok", "detail"}`` so
  clients can render the verdict.

Domain logic lives only here; ``api.agent`` and ``api.admin.routes`` are
thin wrappers that add their own authentication and dispatch.
"""

import os

from ..connectors import registry
from ..connections import records as connections


def _connections_table():
    import boto3

    return boto3.resource("dynamodb").Table(os.environ["CONNECTIONS_TABLE"])


def _connection(connection_id, connections_table=None):
    table = connections_table or _connections_table()
    return connections.get_connection(table, connection_id)


def _resource_entry(discovery):
    """The JSON shape of one discovery resource, as the CLI renders it."""
    params = []
    for param in discovery.params:
        entry = {
            "key": param.get("key"),
            "name": param.get("key"),
            "label": param.get("label") or param.get("key"),
            "type": param.get("type") or "text",
            "required": bool(param.get("required")),
        }
        if param.get("default"):
            entry["default"] = param["default"]
        if param.get("help"):
            entry["help"] = param["help"]
        params.append(entry)
    return {
        "name": discovery.name,
        "connector": discovery.connector,
        "label": discovery.label,
        "description": discovery.description,
        "params": params,
    }


def _load(connection_id, connections_table):
    """``(connection, None)`` or ``(None, (404, payload))`` for the caller."""
    # Consult the table only when one is reachable: a real record wins over
    # the pseudo accounts, but "aws"/"s3" have no record by design and
    # callers may run with no CONNECTIONS_TABLE at all.
    connection = None
    if connections_table is not None or os.environ.get("CONNECTIONS_TABLE"):
        connection = _connection(connection_id, connections_table)
    if not connection:
        connection = _pseudo_connection(connection_id)
    if not connection:
        return None, (404, {"error": f"Unknown connection '{connection_id}'"})
    return connection, None


# "s3"/"aws" name the stored AWS-keys credential, which has no connection
# record but resolves the same bucket discovery and STS health check
# (registry.PROVIDER_DISCOVERY_SOURCES). The synthetic record carries no
# credential_id, so s3's key resolution falls back to the default "aws"
# credential. "mailchimp" resolves the stored Mailchimp API key the same way.
PSEUDO_CONNECTION_PROVIDERS = {"aws": "s3", "s3": "s3", "mailchimp": "mailchimp"}


def _pseudo_connection(connection_id):
    provider = PSEUDO_CONNECTION_PROVIDERS.get(str(connection_id or "").strip().lower())
    if not provider:
        return None
    return {
        "connection_id": str(connection_id).strip().lower(),
        "provider": provider,
        "status": connections.STATUS_CONNECTED,
    }


def resources(connection_id, *, connections_table=None):
    """The discovery resources a connection's provider offers."""
    connection, error = _load(connection_id, connections_table)
    if error:
        return error
    provider = str(connection.get("provider") or "").strip().lower()
    allowed = registry.discoveries_for_provider(provider)
    return 200, {
        "connection": connection_id,
        "provider": provider,
        "resources": [_resource_entry(entry) for entry in allowed],
    }


def _match_discovery(allowed, resource):
    """Resolve a bare resource name or a full ``connector.name`` key."""
    resource = str(resource or "").strip().lower()
    for entry in allowed:
        if f"{entry.connector}.{entry.name}" == resource:
            return entry
    matches = [entry for entry in allowed if entry.name == resource]
    return matches[0] if len(matches) == 1 else None


def discover(connection_id, resource, params, *, connections_table=None):
    """Run one discovery against the connection's provider.

    Returns ``(status, payload)``: 200 with the resolved params and items,
    404 for an unknown connection or resource (the payload lists the valid
    resources), 400 for missing required params, and 502 when the provider
    call fails.
    """
    connection, error = _load(connection_id, connections_table)
    if error:
        return error
    provider = str(connection.get("provider") or "").strip().lower()
    allowed = registry.discoveries_for_provider(provider)
    discovery = _match_discovery(allowed, resource)
    if discovery is None:
        known = ", ".join(entry.name for entry in allowed) or "none"
        return 404, {
            "error": f"Unknown resource '{resource}' for {provider or 'unknown provider'}; "
                     f"known: {known}",
            "resources": [_resource_entry(entry) for entry in allowed],
        }
    if connection.get("status") != connections.STATUS_CONNECTED:
        return 400, {"error": f"Connection {connection_id} is not connected"}

    supplied = {
        str(key): str(value or "").strip()
        for key, value in (params or {}).items()
        if str(value or "").strip()
    }
    resolved = {}
    missing = []
    for param in discovery.params:
        key = str(param.get("key") or "").strip()
        if not key:
            continue
        value = supplied.get(key) or str(param.get("default") or "").strip()
        if value:
            resolved[key] = value
        elif param.get("required"):
            missing.append(key)
    if missing:
        return 400, {"error": f"Missing required parameter(s): {', '.join(sorted(missing))}"}
    try:
        items = discovery.run(connection, resolved)
    except Exception as exc:
        return 502, {"error": f"Discovery failed: {exc}"}
    return 200, {
        "connection": connection_id,
        "resource": discovery.name,
        "params": resolved,
        "items": [item for item in (items or []) if isinstance(item, dict)],
    }


def test_connection(connection_id, *, connections_table=None):
    """Exercise the connection's stored credentials against its provider.

    Always HTTP 200 (except a missing connection): the verdict travels in
    ``ok`` so both UIs render it instead of handling an error path.
    """
    connection, error = _load(connection_id, connections_table)
    if error:
        return error
    provider = str(connection.get("provider") or "").strip().lower()
    test = registry.connection_test_for(provider)
    if test is None:
        return 200, {
            "connection": connection_id,
            "provider": provider,
            "ok": False,
            "detail": "no health check for this provider",
        }
    try:
        verdict = test.run(connection)
    except Exception as exc:
        verdict = {"ok": False, "detail": str(exc) or type(exc).__name__}
    verdict = verdict if isinstance(verdict, dict) else {}
    payload = {
        "connection": connection_id,
        "provider": provider,
        "ok": bool(verdict.get("ok")),
        "detail": str(verdict.get("detail") or ("ok" if verdict.get("ok") else "provider check failed")),
    }
    identity = verdict.get("identity")
    if isinstance(identity, dict):
        payload["identity"] = identity
    acts_as = verdict.get("account_identity")
    if payload["ok"] and isinstance(acts_as, dict):
        payload["account_identity"] = acts_as
        _backfill_identity(connection, acts_as, connections_table)
    return 200, payload


def _backfill_identity(connection, acts_as, connections_table):
    """A passing Test records who the credential acts as on connections
    verified before that was stored (older Slack tokens). Best effort: the
    verdict stands even when the write fails."""
    known = connection.get("account_identity")
    if not connection.get("credential_id") or known == acts_as:
        return
    if isinstance(known, dict) and known.get("name") and not acts_as.get("name"):
        return  # a lookup that came back nameless never erases a known name
    try:
        table = connections_table or _connections_table()
        connections.put_connection(table, {**connection, "account_identity": acts_as})
    except Exception:
        pass
