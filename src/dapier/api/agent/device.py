"""Device pairing (the CLI's `dapier auth login`) and the public
config endpoint the pairing page and CLI read.

The wire is the RFC 8628-shaped grant DataOps uses: the start response
carries absolute verification URIs (the complete form prefills the
page's code field but never approves), the poll speaks the error
vocabulary (400 with ``authorization_pending``/``access_denied``/
``expired_token``, 429 with ``slow_down``), and the browser half reviews
facts (label, IP, age, time left) before a deny-or-approve decision.
"""

import json
import re
import time
import urllib.parse

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


__all__ = ["device_approve", "device_pending", "device_refresh", "device_revoke",
           "device_start", "device_token", "public_config"]



def public_config():
    config = auth_config()
    return _json_response(200, {
        "auth_base_url": config["base_url"],
        "cli_client_id": config["cli_client_id"],
        "issuer": config["issuer"],
        "jwks_url": config["jwks_url"],
        "device_login": True,
    })


def _portal_base(event):
    """The origin the approval page lives on, from the request's host.

    Local development is served over http; everything else is https.
    An empty result leaves the URIs relative — the shared CLI loop
    joins them onto the API URL it already knows.
    """
    headers = event.get("headers") or {}
    def header(name):
        return next((str(v) for k, v in headers.items() if k.lower() == name), "")
    host = header("x-forwarded-host") or header("host")
    if not host:
        return ""
    proto = header("x-forwarded-proto")
    if not proto:
        loopback = re.match(r"^(localhost|127\.0\.0\.1|\[::1\])(:\d+)?$", host)
        proto = "http" if loopback else "https"
    return f"{proto}://{host}"


def device_start(event):
    """Begin a device pairing: secret code for the CLI, human code and
    review facts source for /device."""
    ip = _client_ip(event)
    if not _check_rate_key(("device-start", ip), 10, window=300):
        return _no_store(_json_response(429, {"error": "Rate-limited; retry shortly"}))
    try:
        body = json.loads(event.get("body") or "{}")
    except (ValueError, AttributeError):
        body = None
    label = body.get("label") if isinstance(body, dict) else ""
    device_code, view = device_sessions.start(label=label, request_ip=ip)
    base = _portal_base(event)
    return _no_store(_json_response(200, {
        "device_code": device_code,
        **view,
        "verification_uri": f"{base}/device",
        "verification_uri_complete": f"{base}/device?code={urllib.parse.quote(view['user_code'])}",
    }))


def device_token(event):
    """CLI poll: RFC 8628 outcomes; the approved payload is delivered once."""
    ip = _client_ip(event)
    if not _check_rate_key(("device-poll", ip), 120, window=300):
        return _no_store(_json_response(429, {"error": "slow_down"}, headers={"retry-after": "5"}))
    try:
        body = json.loads(event.get("body") or "{}")
    except (ValueError, AttributeError):
        return _no_store(_json_response(400, {"error": "Invalid request"}))
    result = device_sessions.poll(str(body.get("device_code", "")) if isinstance(body, dict) else "")
    if result.get("error"):
        return _no_store(_json_response(400, result))
    if result.get("token"):
        audit.emit("device-login", audit.DEVICE_LOGIN, result["subject"], outcome="ok")
    return _no_store(_json_response(200, result))


def device_pending(event):
    """What the /device page is about to approve (console session required).

    Review facts only — never the pairing's secrets. Each peek counts
    toward the pairing's attempt budget; a spent pairing reads as a
    404, indistinguishable from an unknown or expired code. Peeks are
    also rate-limited per signed-in account, so one browser cannot sweep
    the code space across many pairings.
    """
    payload = session._session_payload(event)
    if not payload:
        return _no_store(_json_response(401, {"error": "Sign in to approve this device"}))
    subject = payload.get("subject") or payload.get("sub")
    if not _check_rate_key(("device-peek", str(subject)), 20, window=600):
        return _no_store(_json_response(429, {"error": "Too many attempts; wait a few minutes"},
                                        headers={"retry-after": "60"}))
    params = event.get("queryStringParameters") or {}
    facts = device_sessions.lookup(str(params.get("code", "")))
    if not facts:
        return _no_store(_json_response(404, {"error": "Unknown or expired code"}))
    # The page warns (and asks for an acknowledgement) when the browser
    # reviewing the request is not on the network that started it — the
    # device-code phishing shape (dakit device-login.md §4.2).
    caller = _client_ip(event)
    facts["same_network"] = bool(caller) and caller == facts.get("request_ip")
    facts["server_now"] = int(time.time())
    return _no_store(_json_response(200, facts))


def device_approve(event):
    """The human decision from the /device page (console session cookie).

    Cookie-authenticated on purpose: the browser supplies the DTC identity
    that the pairing inherits. Not operator-gated — matching the loopback
    login, any DTC account may pair its own identity; grants still gate
    every action. Deny is exactly as reachable as approve, and the body
    must say which one (``"approve": true|false``) — a decision is never
    inferred. Both outcomes are audited. Never returns the session token;
    the CLI collects it by presenting its secret device code.
    """
    if not session._csrf_ok(event, "POST"):
        return _no_store(_json_response(403, {"error": "Cross-site request rejected"}))
    payload = session._session_payload(event)
    if not payload:
        return _no_store(_json_response(401, {"error": "Sign in to approve this device"}))
    subject = payload.get("subject") or payload.get("sub")
    email = payload.get("sub", "")
    if not _check_rate_key(("device-approve", str(subject)), 10, window=600):
        return _no_store(_json_response(429, {"error": "Too many attempts; wait a few minutes"},
                                        headers={"retry-after": "60"}))
    try:
        body = json.loads(event.get("body") or "{}")
    except (ValueError, AttributeError):
        body = None
    if not isinstance(body, dict) or not isinstance(body.get("approve"), bool):
        return _no_store(_json_response(400, {"error": "Say approve: true or false"}))
    user_code = str(body.get("user_code", ""))
    approve = body["approve"]
    outcome = device_sessions.decide(user_code, approve, subject, email)
    if not outcome:
        audit.emit("device-login", audit.DEVICE_LOGIN, str(subject), outcome="denied-invalid-code")
        return _no_store(_json_response(404, {"error": "That code is not waiting for confirmation."}))
    audit.emit("device-login", audit.DEVICE_LOGIN, str(subject),
               outcome="ok" if outcome["status"] == "approved" else "denied")
    return _no_store(_json_response(200, outcome))


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
    return _no_store(_json_response(200, {"revoked": bool(revoked)}))
