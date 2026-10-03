"""Mailchimp Marketing API v3 provider glue: the one HTTP helper every
Mailchimp call shares, the stored-key resolution, and the audience-webhook
lifecycle behind mailchimp hook triggers.

This is core, not plugin code: triggers.hook_triggers registers and tears
down webhooks on the trigger save/delete path, and core must not import
plugin code (the slack_tokens precedent). The mailchimp plugin's runner and
discovery import the same helper from here.
"""
import base64
import json

DEFAULT_CREDENTIAL_ID = "mailchimp"


def _engine_transport():
    # Resolved at call time, exactly like the runners' ``transport or
    # base._default_transport`` read, so tests patching the engine's
    # default transport keep working.
    from src.dapier.engine.actions import base

    return base._default_transport


def mailchimp_request(method, url, api_key, *, payload=None, transport=None):
    """One Marketing API call: ``(status, parsed body)``, never raising on
    HTTP errors — callers turn status codes into outputs (404 = not found)."""
    transport = transport or _engine_transport()
    headers = {
        "authorization": "Basic " + base64.b64encode(f"anystring:{api_key}".encode()).decode(),
        "accept": "application/json",
    }
    body = None
    if payload is not None:
        headers["content-type"] = "application/json"
        body = json.dumps(payload, separators=(",", ":")).encode()
    try:
        status, raw = transport(method, url, headers=headers, body=body, timeout=15)
    except Exception as exc:
        raise RuntimeError(f"Mailchimp unreachable: {type(exc).__name__}") from None
    if isinstance(raw, dict):
        data = raw
    else:
        try:
            data = json.loads(raw) if raw else {}
        except (ValueError, TypeError):
            data = {}
    return status, data if isinstance(data, dict) else {}


def stored_settings(connection):
    """The (api_key, server) pair behind the connection, falling back to the
    shared ``mailchimp`` credential — mirroring the s3 key resolution."""
    from src.dapier.connections import credentials

    for credential_id in ((connection or {}).get("credential_id"), DEFAULT_CREDENTIAL_ID):
        if not credential_id:
            continue
        try:
            secret = credentials.get_credential(credential_id)
        except KeyError:
            continue
        api_key = str(secret.get("apiKey") or secret.get("api_key") or "").strip()
        server = str(secret.get("server") or "").strip()
        if api_key and not server and "-" in api_key:
            server = api_key.rsplit("-", 1)[-1]
        if api_key and server:
            return api_key, server
    raise RuntimeError("no stored Mailchimp API key for this connection")


def register_webhook(list_id, url, events, api_key, server, *, transport=None):
    """Subscribe one webhook on an audience; returns the created id.

    ``events`` maps each intake type (subscribe, unsubscribe, ...) to whether
    it is subscribed; every source is subscribed (user, admin, api) so
    changes made through the Marketing API itself — like the upsert action —
    fire too. A rejection raises RuntimeError with Mailchimp's detail.
    """
    status, data = mailchimp_request(
        "POST", f"https://{server}.api.mailchimp.com/3.0/lists/{list_id}/webhooks",
        api_key, payload={"url": url, "events": dict(events),
                          "sources": {"user": True, "admin": True, "api": True}},
        transport=transport)
    if status >= 300:
        detail = str(data.get("detail") or data.get("title") or "") if isinstance(data, dict) else ""
        raise RuntimeError(f"Mailchimp rejected the webhook registration: {detail or f'HTTP {status}'}")
    return str(data.get("id") or "")


def list_webhooks(list_id, api_key, server, *, transport=None):
    """The audience's registered webhooks (``{id, url, ...}`` dicts)."""
    status, data = mailchimp_request(
        "GET", f"https://{server}.api.mailchimp.com/3.0/lists/{list_id}/webhooks",
        api_key, transport=transport)
    if status >= 300:
        raise RuntimeError(f"Mailchimp webhook listing returned HTTP {status}")
    return [entry for entry in data.get("webhooks") or [] if isinstance(entry, dict)]


def delete_webhook(list_id, webhook_id, api_key, server, *, transport=None):
    """Remove one webhook by id; a rejection raises RuntimeError."""
    status, data = mailchimp_request(
        "DELETE", f"https://{server}.api.mailchimp.com/3.0/lists/{list_id}/webhooks/{webhook_id}",
        api_key, transport=transport)
    if status >= 300:
        detail = str(data.get("detail") or data.get("title") or "") if isinstance(data, dict) else ""
        raise RuntimeError(f"Mailchimp webhook removal failed: {detail or f'HTTP {status}'}")
