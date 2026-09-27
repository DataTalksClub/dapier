"""Webhook connector: signed outbound POSTs and the generic HTTP request."""
import base64
import json

from ..engine.actions import base
from ..engine.actions.templating import render
from ..engine.actions.webhook import run_webhook
from .registry import Action, register

HTTP_METHODS = ("GET", "POST", "PUT", "PATCH", "DELETE")
AUTH_TYPES = ("none", "basic", "bearer", "api_key")

register(Action(
    type="webhook",
    label="Webhook",
    icon="webhook",
    run=lambda action, event, workflow_id, steps=None: run_webhook(action, event, steps=steps),
    required=frozenset({"url"}),
    optional=frozenset({"payload", "secret_id", "timeout_seconds"}),
    fields=(
        {"key": "url", "label": "URL", "type": "url", "required": True},
        {"key": "payload", "label": "Payload (JSON, templated)", "type": "textarea",
         "placeholder": '{"id": "{trigger.id}"}'},
        {"key": "secret_id", "label": "Signing secret ID", "placeholder": "dapier/webhook"},
        {"key": "timeout_seconds", "label": "Timeout (s)", "type": "number"},
    ),
))


def _connection_token(connection_id):
    """The bearer token behind a connected connection, for request auth."""
    connection = base._connected_connection(connection_id)
    from ..connections import credentials

    secret = credentials.get_credential(connection["credential_id"])
    token = secret.get("token") or secret.get("access_token")
    if not token:
        raise ValueError(f"connection {connection_id} has no stored token")
    return token


def run_http_request(action, event, *, steps=None, transport=None):
    """Call any HTTP endpoint: templated URL/headers/body, and a choice of
    auth — basic, bearer (direct or from a connection) or an API key in a
    header or the query string.

    The parsed JSON response body (or a text preview) lands in the step
    output, so later steps can template ``{steps.<id>.output.body.<path>}``.
    """
    method = str(action.get("method") or "GET").upper()
    if method not in HTTP_METHODS:
        raise ValueError(f"http_request method must be one of: {', '.join(HTTP_METHODS)}")
    url = render(action["url"], event, steps)
    headers = {
        str(name): render(str(value), event, steps)
        for name, value in (action.get("headers") or {}).items()
    }
    auth_type = str(action.get("auth_type") or "none").lower()
    if auth_type not in AUTH_TYPES:
        raise ValueError(f"http_request auth_type must be one of: {', '.join(AUTH_TYPES)}")
    if auth_type == "none":
        if action.get("connection_id"):
            headers.setdefault("authorization", f"Bearer {_connection_token(action['connection_id'])}")
    else:
        url = _apply_auth(auth_type, action, url, event, steps, headers)
    body = None
    if str(action.get("body") or "").strip():
        headers.setdefault("content-type", action.get("content_type") or "application/json")
        body = render(action["body"], event, steps).encode()
    timeout = action.get("timeout_seconds", 15)
    if transport is not None:
        status, raw = transport(method, url, headers=headers, body=body, timeout=timeout)
    else:
        status, raw = base._default_transport(method, url, headers=headers, body=body, timeout=timeout)
    if status >= 300:
        raise RuntimeError(f"http_request returned HTTP {status}")
    return {"status": status, "body": _decode_body(raw)}


def _apply_auth(auth_type, action, url, event, steps, headers):
    """Set the request credential for an explicit ``auth_type``.

    Returns the (possibly rewritten) URL — an API key placed in the query is
    appended there instead of the headers.
    """
    if auth_type == "basic":
        username = render(action.get("auth_username") or "", event, steps)
        password = render(action.get("auth_password") or "", event, steps)
        if not username or not password:
            raise ValueError("http_request basic auth needs auth_username and auth_password")
        encoded = base64.b64encode(f"{username}:{password}".encode()).decode()
        headers["authorization"] = f"Basic {encoded}"
    elif auth_type == "bearer":
        token = render(action.get("auth_token") or "", event, steps).strip()
        if not token and action.get("connection_id"):
            token = _connection_token(action["connection_id"])
        if not token:
            raise ValueError("http_request bearer auth needs auth_token or connection_id")
        headers["authorization"] = f"Bearer {token}"
    else:
        name = render(action.get("auth_key_name") or "", event, steps).strip()
        value = render(action.get("auth_key_value") or "", event, steps)
        if not name or not value:
            raise ValueError("http_request api_key auth needs auth_key_name and auth_key_value")
        placement = str(action.get("auth_key_in") or "header").lower()
        if placement == "header":
            headers[name.lower()] = value
        elif placement == "query":
            url += ("&" if "?" in url else "?") + f"{name}={value}"
        else:
            raise ValueError("http_request auth_key_in must be header or query")
    return url


def _decode_body(raw, limit=4000):
    try:
        text = raw.decode()
    except (AttributeError, UnicodeDecodeError):
        return str(raw)[:limit]
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return text[:limit]


register(Action(
    type="http_request",
    label="HTTP request",
    icon="globe",
    description="Call any API: templated URL, headers and body; basic, bearer or API-key auth",
    run=lambda action, event, workflow_id, steps=None: run_http_request(action, event, steps=steps),
    required=frozenset({"url"}),
    optional=frozenset({"method", "headers", "body", "content_type", "connection_id",
                        "auth_type", "auth_username", "auth_password", "auth_token",
                        "auth_key_name", "auth_key_value", "auth_key_in",
                        "timeout_seconds"}),
    fields=(
        {"key": "url", "label": "URL", "type": "url", "required": True,
         "placeholder": "https://api.example.com/items/{id}"},
        {"key": "method", "label": "Method", "type": "select", "options": list(HTTP_METHODS), "default": "GET"},
        {"key": "auth_type", "label": "Auth", "type": "select", "options": list(AUTH_TYPES),
         "default": "none"},
        {"key": "auth_username", "label": "Basic username"},
        {"key": "auth_password", "label": "Basic password"},
        {"key": "auth_token", "label": "Bearer token", "placeholder": "or a connection ID"},
        {"key": "connection_id", "label": "Connection ID", "placeholder": "bearer token fallback"},
        {"key": "auth_key_name", "label": "API key name", "placeholder": "x-api-key"},
        {"key": "auth_key_value", "label": "API key value"},
        {"key": "auth_key_in", "label": "API key in", "type": "select", "options": ["header", "query"]},
        {"key": "headers", "label": "Headers (YAML)", "type": "textarea",
         "placeholder": 'accept: application/json\nx-trace: "{trigger.id}"'},
        {"key": "body", "label": "Body template", "type": "textarea",
         "placeholder": '{"subject": "{subject}"}'},
        {"key": "content_type", "label": "Content type", "placeholder": "application/json"},
        {"key": "timeout_seconds", "label": "Timeout (s)", "type": "number"},
    ),
))


# --- trigger discovery: the newest recorded request, else a realistic sample ---

from . import trigger_discovery
from .trigger_discovery import (
    DEFAULT_LIMIT,
    TriggerDiscovery,
    history_or_synthetic_fetch,
    register_trigger_discovery,
)

# The data shape a webhook delivery publishes (see api.router._webhook_hook).
_WEBHOOK_SYNTHETIC_DATA = {
    "hook": "orders",
    "body": {"order_id": "4137", "total": "42.00", "currency": "EUR"},
    "query": {"source": "checkout"},
    "content_type": "application/json",
}


register_trigger_discovery(TriggerDiscovery(
    connector="webhook", label="Webhook", kind="sample", resource="",
    # hook_triggers.WEBHOOK_EVENT; inlined to keep this module import-light.
    fetch=history_or_synthetic_fetch("webhook", "request.received", _WEBHOOK_SYNTHETIC_DATA)))
