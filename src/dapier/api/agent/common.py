"""Shared request plumbing for the agent API: JSON responses, rate
limiting, bearer authentication, and the operator gates every
route module builds on."""

import base64
import json
import os
import time

__all__ = ["RATE_WINDOW_SECONDS", "authenticate", "require_operator", "reset_rate_limits"]



class _LateBinding:
    """A shared dependency resolved through the agent package at call time,
    so tests that patch agent.<name> are honored by every route module."""

    def __init__(self, name):
        self._name = name

    def __getattr__(self, attr):
        import importlib

        return getattr(getattr(importlib.import_module(__package__), self._name), attr)

    def __call__(self, *args, **kwargs):
        import importlib

        return getattr(importlib.import_module(__package__), self._name)(*args, **kwargs)


from ...auth import api_tokens, authz, device_sessions, visibility
from ...auth.dtc_auth import auth_config
from .. import designer_store

audit = _LateBinding("audit")
verify_id_token = _LateBinding("verify_id_token")


RATE_WINDOW_SECONDS = 60

_BUCKETS = {}




def _json_response(status, body, *, headers=None):
    from ... import http as http_helpers

    return {
        "statusCode": status,
        "headers": {"content-type": "application/json", **(headers or {})},
        # Connection records carry DynamoDB Decimals (version, timestamps);
        # a plain dumps 500s every view that returns them.
        "body": json.dumps(body, default=http_helpers._json_default),
    }


def _no_store(body_status):
    body_status["headers"]["cache-control"] = "no-store"
    return body_status


def reset_rate_limits():
    _BUCKETS.clear()


def _rate_limit():
    try:
        return max(int(os.environ.get("TOKEN_RATE_LIMIT", "30")), 1)
    except ValueError:
        return 30


def _check_rate(subject, connection_id, *, now=None):
    return _check_rate_key((subject, connection_id), _rate_limit(), now=now)


def _check_rate_key(key, limit, *, window=RATE_WINDOW_SECONDS, now=None):
    now = now if now is not None else time.time()
    window_start = now - window
    hits = [moment for moment in _BUCKETS.get(key, []) if moment > window_start]
    if len(hits) >= limit:
        _BUCKETS[key] = hits
        return False
    _BUCKETS[key] = hits + [now]
    return True


def _client_ip(event):
    return (event.get("requestContext", {}).get("http", {}) or {}).get("sourceIp", "")


def _bearer(event):
    headers = event.get("headers") or {}
    value = next(
        (str(v) for k, v in headers.items() if k.lower() == "authorization"), "",
    )
    scheme, _, token = value.partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        return ""
    return token.strip()


def authenticate(event):
    """Return ``(subject, None)`` or ``(None, error_response)``.

    Three bearer kinds: a Dapier-issued API token (``dap_…``) authenticates
    as its machine subject, a device session (``dapd_…``) as the operator
    DTC subject that approved the pairing, and any other value is verified
    as a DTC ID token. API tokens work even where CLI token issuance is
    unconfigured.
    """
    token = _bearer(event)
    if not token:
        return None, _json_response(401, {"error": "DTC identity required"})
    if token.startswith(api_tokens.PREFIX):
        item = api_tokens.verify(token)
        if not item:
            return None, _json_response(401, {"error": "Invalid API token"})
        event["_api_token"] = item
        api_tokens.mark_used(item["token_hash"])
        return item["subject"], None

    if token.startswith(device_sessions.PREFIX):
        paired = device_sessions.resolve(token)
        if not paired:
            return None, _json_response(401, {"error": "Invalid Dapier device session"})
        event["_dtc_claims"] = {"sub": paired["subject"], "email": paired["email"]}
        return str(paired["subject"]), None

    cli_client_id = auth_config().get("cli_client_id", "")
    if not cli_client_id:
        return None, _json_response(503, {"error": "CLI token issuance is not configured"})
    try:
        claims = verify_id_token(token, audience=cli_client_id)
    except Exception:
        return None, _json_response(401, {"error": "Invalid DTC identity"})
    subject = claims.get("sub")
    if not subject:
        return None, _json_response(401, {"error": "Invalid DTC identity"})
    event["_dtc_claims"] = claims
    return str(subject), None


def _is_operator(event, subject):
    """Operator check that API tokens can never pass.

    An empty operator allowlist means every DTC-authenticated account may
    administer the console; a machine token must not inherit that, so the
    check is DTC-claims-only by construction.
    """
    if event.get("_api_token"):
        return False
    claims = event.get("_dtc_claims") or {}
    return authz.is_operator({"subject": subject, "sub": claims.get("email", "")})


def _api_token(event):
    return event.get("_api_token")


def _check_agent_binding(event, agent):
    """Refuse API-token callers acting as any agent other than their own."""
    item = _api_token(event)
    return bool(item) and agent != item.get("agent")


def _tables():
    import boto3

    resource = boto3.resource("dynamodb")
    return (
        resource.Table(os.environ["CONNECTIONS_TABLE"]),
        resource.Table(os.environ["GRANTS_TABLE"]),
    )


def require_operator(event, action):
    """Authenticate the bearer identity and require the operator allowlist.

    Returns ``(subject, None)`` or ``(None, error_response)``. ``action`` is
    the audit action recorded on a denial. A DTC identity passes the same
    operator allowlist the console gate applies; API tokens pass only
    through their grants."""
    subject, error = authenticate(event)
    if error:
        return None, error
    if event.get("_api_token"):
        if _is_operator(event, subject):
            return subject, None
        audit.emit("unknown", action, subject, outcome="denied-not-operator")
        return None, _json_response(403, {"error": "Operator authorization required"})
    claims = event.get("_dtc_claims") or {}
    payload = {"subject": subject, "sub": claims.get("email", "")}
    if authz.is_operator(payload):
        return subject, None
    audit.emit("unknown", action, subject, outcome="denied-not-operator")
    return None, _json_response(403, {"error": "Operator authorization required"})


def _visibility(event, subject):
    """The caller's G17 Phase 2 read scope, decided exactly as
    require_operator decides its gate: an API token passes only as an
    operator, a DTC identity resolves through the operator allowlist —
    operators see everything, anyone else is owner-scoped to their
    subject (auth.visibility). Writes gate through the same scope
    (_write_denied, Phase 3)."""
    if event.get("_api_token"):
        return visibility.Visibility(subject, is_operator=True)
    claims = event.get("_dtc_claims") or {}
    return visibility.for_session({"subject": subject, "sub": claims.get("email", "")})


def _write_denied(event, subject, workflow_id, action):
    """G17 Phase 3: the owner-or-operator write gate for the workflow-scoped
    routes — a 403 response when a non-operator targets a workflow it does
    not own (visibility.ensure_can_write holds the rule: a create stays
    open, an ownerless stored item does not), else None. Same verdict as
    require_operator/_visibility; denials are audited like role denials."""
    denied = _visibility(event, subject).can_write(workflow_id)
    if denied is None:
        return None
    audit.emit(str(workflow_id or "unknown"), action, subject,
               outcome="denied-not-owner")
    return _json_response(*denied)


def _save_denied(event, subject, body):
    """The save gate over every id a save body touches (the definition's id
    plus a renameFrom target), in the gate's own order."""
    for workflow_id in designer_store.save_gate_ids(body):
        denied = _write_denied(event, subject, workflow_id, "workflow.save")
        if denied is not None:
            return denied
    return None

def _b64encode(value):
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode()
