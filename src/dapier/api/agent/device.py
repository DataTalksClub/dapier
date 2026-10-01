"""Device pairing (the CLI's `dapier auth login`) and the public
config endpoint the pairing page and CLI read."""

import json

from ...auth import device_sessions, session
from ...auth.dtc_auth import auth_config

from .common import _json_response, _no_store, _check_rate_key, _client_ip, _bearer

from .common import _LateBinding

# Shared dependencies resolved through the agent package at call time:
# tests patch agent.<name> and every route module must see the patch.
authenticate = _LateBinding("authenticate")
require_operator = _LateBinding("require_operator")
_is_operator = _LateBinding("_is_operator")
_tables = _LateBinding("_tables")
audit = _LateBinding("audit")
verify_id_token = _LateBinding("verify_id_token")


__all__ = ["device_confirm", "device_refresh", "device_revoke", "device_start", "device_token", "public_config"]



def public_config():
    config = auth_config()
    return _json_response(200, {
        "auth_base_url": config["base_url"],
        "cli_client_id": config["cli_client_id"],
        "issuer": config["issuer"],
        "jwks_url": config["jwks_url"],
        "device_login": True,
    })


def device_start(event):
    """Begin a device pairing: secret code for the CLI, human code for /device."""
    ip = _client_ip(event)
    if not _check_rate_key(("device-start", ip), 10, window=300):
        return _no_store(_json_response(429, {"error": "Rate-limited; retry shortly"}))
    device_code, view = device_sessions.start()
    return _no_store(_json_response(200, {
        "device_code": device_code,
        "verification_uri": "/device",
        **view,
    }))


def device_token(event):
    """CLI poll: pending until approved, expired after the TTL, approved once."""
    ip = _client_ip(event)
    if not _check_rate_key(("device-poll", ip), 120, window=300):
        return _no_store(_json_response(429, {"error": "Rate-limited; retry shortly"}))
    try:
        body = json.loads(event.get("body") or "{}")
    except (ValueError, AttributeError):
        return _no_store(_json_response(400, {"error": "Invalid request"}))
    result = device_sessions.poll(str(body.get("device_code", "")) if isinstance(body, dict) else "")
    if result["status"] == "approved":
        audit.emit("device-login", audit.DEVICE_LOGIN, result["subject"], outcome="ok")
    return _no_store(_json_response(200, result))


def device_confirm(event):
    """Approve a pairing from the /device page (console session cookie).

    Cookie-authenticated on purpose: the browser supplies the DTC identity
    that the pairing inherits. Not operator-gated — matching the loopback
    login, any DTC account may pair its own identity; grants still gate
    every action. Never returns the session token; the CLI collects it by
    presenting its secret device code.
    """
    if not session._csrf_ok(event, "POST"):
        return _json_response(403, {"error": "Cross-site request rejected"})
    payload = session._session_payload(event)
    if not payload:
        return _json_response(401, {"error": "Sign in to approve this device"})
    subject = payload.get("subject") or payload.get("sub")
    email = payload.get("sub", "")
    if not _check_rate_key(("device-confirm", str(subject)), 10, window=600):
        return _json_response(429, {"error": "Too many attempts; wait a few minutes"})
    try:
        body = json.loads(event.get("body") or "{}")
    except (ValueError, AttributeError):
        return _json_response(400, {"error": "Invalid request"})
    outcome = device_sessions.approve(
        body.get("user_code", "") if isinstance(body, dict) else "",
        subject, email,
    )
    if outcome != "ok":
        audit.emit("device-login", audit.DEVICE_LOGIN, str(subject), outcome="denied-invalid-code")
        return _json_response(400, {"error": "Unknown or expired code"})
    audit.emit("device-login", audit.DEVICE_LOGIN, str(subject), outcome="ok")
    return _json_response(200, {"status": "approved"})


def device_refresh(event):
    """Rotate the caller's device session; returns the replacement token."""
    token = _bearer(event)
    rotated = device_sessions.refresh(token) if token.startswith(device_sessions.PREFIX) else None
    if not rotated:
        return _no_store(_json_response(401, {"error": "Invalid Dapier device session"}))
    new_token, expires_at = rotated
    return _no_store(_json_response(200, {
        "token": new_token, "expires_at": expires_at,
    }))


def device_revoke(event):
    """Revoke the caller's device session (dapier auth logout)."""
    token = _bearer(event)
    revoked = device_sessions.revoke(token) if token.startswith(device_sessions.PREFIX) else False
    return _json_response(200, {"revoked": bool(revoked)})
