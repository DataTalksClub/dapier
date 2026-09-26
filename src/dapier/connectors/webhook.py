"""Webhook connector: signed outbound POSTs and the generic HTTP request."""
import json

from ..engine.actions import base
from ..engine.actions.templating import render
from ..engine.actions.webhook import run_webhook
from .registry import Action, register

HTTP_METHODS = ("GET", "POST", "PUT", "PATCH", "DELETE")

register(Action(
    type="webhook",
    label="Webhook",
    icon="webhook",
    run=lambda action, event, workflow_id, steps=None: run_webhook(action, event),
    required=frozenset({"url"}),
    optional=frozenset({"secret_id", "timeout_seconds"}),
    fields=(
        {"key": "url", "label": "URL", "required": True},
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
    """Call any HTTP endpoint: templated URL/headers/body, optional bearer
    auth from a connection.

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
    if action.get("connection_id"):
        headers.setdefault("authorization", f"Bearer {_connection_token(action['connection_id'])}")
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
    description="Call any API: templated URL, headers and body; optional bearer auth from a connection",
    run=lambda action, event, workflow_id, steps=None: run_http_request(action, event, steps=steps),
    required=frozenset({"url"}),
    optional=frozenset({"method", "headers", "body", "content_type", "connection_id",
                        "timeout_seconds"}),
    fields=(
        {"key": "url", "label": "URL", "required": True, "placeholder": "https://api.example.com/items/{id}"},
        {"key": "method", "label": "Method", "type": "select", "options": list(HTTP_METHODS), "default": "GET"},
        {"key": "connection_id", "label": "Connection ID", "placeholder": "bearer token for the request"},
        {"key": "headers", "label": "Headers (YAML)", "type": "textarea",
         "placeholder": 'accept: application/json\nx-trace: "{trigger.id}"'},
        {"key": "body", "label": "Body template", "type": "textarea",
         "placeholder": '{"subject": "{subject}"}'},
        {"key": "content_type", "label": "Content type", "placeholder": "application/json"},
        {"key": "timeout_seconds", "label": "Timeout (s)", "type": "number"},
    ),
))
