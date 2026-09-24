"""Operator browser sessions: signed cookies, CSRF, and the operator gate."""
import base64
import hashlib
import hmac
import json
import os
import time
import urllib.parse

import boto3

from .. import audit as audit_log
from .. import http
from . import authz

SESSION_COOKIE = "dapier_session"
OAUTH_COOKIE = "dapier_oauth_state"
AUTH_STATE_COOKIE = "dapier_auth_state"
SESSION_TTL_SECONDS = 12 * 60 * 60

_admin_secret = None


def _credentials():
    # The generated "password" is the HMAC key for signed session cookies.
    global _admin_secret
    if _admin_secret is None:
        value = boto3.client("secretsmanager").get_secret_value(
            SecretId=os.environ["ADMIN_SECRET_ID"]
        )["SecretString"]
        _admin_secret = json.loads(value)
    return _admin_secret

def _b64encode(value):
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode()

def _b64decode(value):
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))

def _sign(payload):
    encoded = _b64encode(json.dumps(payload, separators=(",", ":"), sort_keys=True).encode())
    signature = hmac.new(_credentials()["password"].encode(), encoded.encode(), hashlib.sha256).digest()
    return f"{encoded}.{_b64encode(signature)}"

def _verify(token, kind="session"):
    try:
        encoded, supplied = token.split(".", 1)
        expected = hmac.new(
            _credentials()["password"].encode(), encoded.encode(), hashlib.sha256
        ).digest()
        if not hmac.compare_digest(_b64decode(supplied), expected):
            return None
        payload = json.loads(_b64decode(encoded))
        if payload.get("kind", "session") != kind or payload.get("exp", 0) < int(time.time()):
            return None
        return payload
    except (ValueError, KeyError, TypeError, json.JSONDecodeError):
        return None

def _cookie(event, name):
    cookies = event.get("cookies") or []
    if not cookies:
        raw = _header(event, "cookie")
        cookies = [part.strip() for part in raw.split(";") if part.strip()]
    prefix = f"{name}="
    return next((part[len(prefix):] for part in cookies if part.startswith(prefix)), "")

def _header(event, name):
    expected = name.lower()
    return next(
        (str(value) for key, value in (event.get("headers") or {}).items() if key.lower() == expected),
        "",
    )

def _session_payload(event):
    return _verify(_cookie(event, SESSION_COOKIE)) or {}

def authenticated(event):
    return _verify(_cookie(event, SESSION_COOKIE)) is not None

def require_operator(event):
    """Return ``(payload, None)`` for operators, ``(None, response)`` otherwise."""
    payload = _session_payload(event)
    if not payload:
        return None, http._json_response(401, {"error": "Authentication required"})
    if not authz.is_operator(payload):
        _audit_event(
            "unknown", audit_log.CONNECT, subject_fallback(payload),
            outcome="denied-not-operator",
        )
        return None, http._json_response(403, {"error": "Operator authorization required"})
    return payload, None

def subject_fallback(payload):
    subject = (payload or {}).get("subject") or (payload or {}).get("sub")
    return str(subject) if subject else "unknown"

def _audit_event(connection_id, action, actor, *, outcome, agent=None, error=None):
    return audit_log.emit(
        connection_id, action, actor, outcome=outcome, agent=agent, error=error,
    )

def _csrf_ok(event, method):
    """Same-origin check for cookie-authenticated state-changing requests."""
    if method in ("GET", "HEAD", "OPTIONS"):
        return True
    if not _cookie(event, SESSION_COOKIE):
        return True
    host = _header(event, "host").lower()
    for header in (_header(event, "origin"), _header(event, "referer")):
        if not header:
            continue
        try:
            if urllib.parse.urlparse(header).hostname.lower() == host:
                return True
        except ValueError:
            continue
    return False

def _session_subject(event):
    payload = _session_payload(event)
    subject = payload.get("subject") or payload.get("sub")
    return str(subject) if subject else None
