"""DTC operator sign-in: the /auth/* browser flow."""
import hashlib
import hmac
import os
import time
import urllib.parse

from ...auth import dtc_auth
from ...auth import session
from ... import http
from ...auth.session import SESSION_COOKIE, AUTH_STATE_COOKIE, SESSION_TTL_SECONDS


def _safe_next(raw):
    """Where to land after sign-in: any in-app path, never off-site."""
    value = str(raw or "")
    if value.startswith("/") and not value.startswith("//") and "\\" not in value:
        return value
    return "/"


def auth_login(event):
    config = dtc_auth.auth_config()
    if not all(config[key] for key in ("base_url", "client_id", "callback_url", "issuer", "jwks_url")):
        return http._json_response(503, {"error": "Shared authentication is not configured"})
    next_path = _safe_next((event.get("queryStringParameters") or {}).get("next"))
    state = session._b64encode(os.urandom(32))
    verifier = session._b64encode(os.urandom(48))
    nonce = session._b64encode(os.urandom(32))
    challenge = session._b64encode(hashlib.sha256(verifier.encode()).digest())
    query = urllib.parse.urlencode({
        "response_type": "code", "client_id": config["client_id"],
        "redirect_uri": config["callback_url"], "scope": "openid email profile",
        "state": state, "nonce": nonce, "code_challenge": challenge,
        "code_challenge_method": "S256",
    })
    token = session._sign({
        "kind": "oidc", "state": state, "verifier": verifier, "nonce": nonce,
        "next": next_path, "exp": int(time.time()) + 600,
    })
    return http._redirect(
        f'{config["base_url"]}/oauth2/authorize?{query}',
        cookies=[f"{AUTH_STATE_COOKIE}={token}; Path=/auth/callback; Max-Age=600; HttpOnly; Secure; SameSite=Lax"],
    )

def auth_callback(event):
    pending = session._verify(session._cookie(event, AUTH_STATE_COOKIE), kind="oidc")
    query = event.get("queryStringParameters") or {}
    code, state = query.get("code", ""), query.get("state", "")
    clear_state = f"{AUTH_STATE_COOKIE}=; Path=/auth/callback; Max-Age=0; HttpOnly; Secure; SameSite=Lax"
    if not pending or not code or not hmac.compare_digest(state, pending.get("state", "")):
        return _auth_error_redirect(clear_state)
    try:
        claims = dtc_auth.verify_id_token(dtc_auth.exchange_auth_code(code, pending["verifier"])["id_token"])
    except Exception:
        return _auth_error_redirect(clear_state)
    if not hmac.compare_digest(str(claims.get("nonce", "")), pending.get("nonce", "")):
        return _auth_error_redirect(clear_state)
    # `email_verified` is not re-checked here: the shared pool's pre-sign-up trigger
    # already requires a verified @datatalks.club address on every Google
    # authentication, and this app client is Google-only. The email claim itself is
    # still required, because the session is keyed by it.
    email = claims.get("email")
    if not isinstance(email, str) or not email:
        return _auth_error_redirect(clear_state)
    token = session._sign({"sub": email.lower(), "subject": claims["sub"], "exp": int(time.time()) + SESSION_TTL_SECONDS})
    return http._redirect(
        _safe_next(pending.get("next")),
        cookies=[clear_state, f"{SESSION_COOKIE}={token}; Path=/; Max-Age={SESSION_TTL_SECONDS}; HttpOnly; Secure; SameSite=Lax"],
    )

def _auth_error_redirect(clear_state):
    return http._redirect(
        "/auth/error",
        status=303,
        cookies=[clear_state],
        headers={"cache-control": "no-store", "referrer-policy": "no-referrer"},
    )

def auth_error():
    return {
        "statusCode": 403,
        "headers": {
            "content-type": "text/html; charset=utf-8",
            "cache-control": "no-store",
            "referrer-policy": "no-referrer",
            "x-content-type-options": "nosniff",
            "content-security-policy": "default-src 'none'; style-src 'unsafe-inline'; base-uri 'none'; frame-ancestors 'none'",
        },
        "body": """<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Sign-in error · Dapier</title><style>:root{color-scheme:light}body{margin:0;min-height:100vh;display:grid;align-items:center;justify-items:center;background:#eeede6;color:#202820;font:14px/1.5 "IBM Plex Sans",ui-sans-serif,system-ui,"Segoe UI",sans-serif}main{width:min(400px,calc(100% - 48px))}.wordmark{margin:0 0 6px;font-size:34px;font-weight:600;letter-spacing:-.04em}.sub{margin:0 0 26px;color:#667166;font-size:13px}.card{border-top:1px solid #879688;padding-top:24px;display:grid;gap:14px}h1{margin:0;font-size:18px;font-weight:600}p{margin:0;color:#485348;font-size:13.5px}a{color:#0b6745;font-weight:500;text-underline-offset:3px}.foot{margin-top:26px;color:#667166;font-size:11.5px;font-family:"IBM Plex Mono",ui-monospace,Menlo,Consolas,monospace}</style></head><body><main><p class="wordmark">Dapier</p><p class="sub">Operator console for AWS automation</p><div class="card"><h1>Sign-in error</h1><p>Authentication could not be completed. No account changes were made.</p><p><a href="/auth/login">Try again</a></p></div><p class="foot">eu-west-1 · single-operator control plane</p></main></body></html>""",
    }

def auth_logout():
    config = dtc_auth.auth_config()
    location = "/"
    if config["base_url"] and config["client_id"] and config["logout_url"]:
        query = urllib.parse.urlencode({"client_id": config["client_id"], "logout_uri": config["logout_url"]})
        location = f'{config["base_url"]}/logout?{query}'
    return http._redirect(location, cookies=[f"{SESSION_COOKIE}=; Path=/; Max-Age=0; HttpOnly; Secure; SameSite=Lax"])
