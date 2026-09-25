"""DTC shared-auth login for the CLI.

Default flow: device pairing (`login_device`) — the CLI shows a short code,
the operator approves it on the console's /device page after signing in
with the DTC identity, and the CLI polls for a dapier-issued device
session. No localhost listener is needed. The classic browser loopback
flow (`login`, used by `dapier auth login --browser`) signs in directly
against the shared DTC issuer and stores DTC-issued tokens instead.
"""

import base64
import hashlib
import json
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer

from . import config

LOOPBACK_HOST = "127.0.0.1"
# Cognito accepts cleartext loopback redirects only as http://localhost —
# never 127.0.0.1, never an ephemeral port — so the callback server binds
# this exact port to match the URL registered in the shared-auth stack.
LOOPBACK_PORT = 8471
REDIRECT_URI = f"http://localhost:{LOOPBACK_PORT}/callback"
SCOPES = "openid email profile"


def _random(bytes_count):
    return base64.urlsafe_b64encode(os.urandom(bytes_count)).rstrip(b"=").decode()


def build_pkce():
    verifier = _random(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    return verifier, challenge


def authorize_url(auth_base_url, client_id, redirect_uri, state, nonce, challenge):
    query = urllib.parse.urlencode({
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "scope": SCOPES,
        "state": state,
        "nonce": nonce,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    })
    return f"{auth_base_url.rstrip('/')}/oauth2/authorize?{query}"


def _post_form(url, fields, timeout=20):
    body = urllib.parse.urlencode(fields).encode()
    request = urllib.request.Request(
        url, data=body,
        headers={"content-type": "application/x-www-form-urlencoded"}, method="POST",
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.status, response.read()


def _get_json(url, timeout=20):
    with urllib.request.urlopen(url, timeout=timeout) as response:
        return json.loads(response.read())


def server_config(api_url):
    """Fetch the public agent config (auth endpoints + CLI client ID)."""
    data = _get_json(f"{api_url}/api/agent/config")
    if not isinstance(data, dict) or not data.get("cli_client_id"):
        raise LoginError("This Dapier API does not have CLI token issuance configured")
    for key in ("auth_base_url", "issuer", "jwks_url"):
        if not data.get(key):
            raise LoginError("This Dapier API returned an incomplete agent config")
    return data


class LoginError(Exception):
    pass


class DeviceFlowUnavailable(Exception):
    """The API predates device pairing; fall back to the browser flow."""


def _post_json(url, body, bearer=None, timeout=20):
    """POST JSON, returning ``(status, parsed_body)`` without leaking tokens."""
    headers = {"content-type": "application/json"}
    if bearer:
        headers["authorization"] = f"Bearer {bearer}"
    request = urllib.request.Request(
        url, data=json.dumps(body).encode(), headers=headers, method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, json.loads(response.read() or b"{}")
    except urllib.error.HTTPError as exc:
        try:
            data = json.loads(exc.read() or b"{}")
        except ValueError:
            data = {}
        if exc.code in (404, 503) and url.endswith("/device/start"):
            raise DeviceFlowUnavailable from None
        return exc.code, data


def _wait_seconds(timeout, expires_in):
    """Never give up before the code does: wait out the pairing's full life."""
    if expires_in:
        return max(timeout, int(expires_in) + 15)
    return timeout


def login_device(api_url, *, timeout=900, sleeper=time.sleep, poster=None):
    """Pair this CLI with the operator identity; returns the device session."""
    poster = poster or _post_json
    base = api_url.rstrip("/")
    status, pairing = poster(f"{base}/api/agent/device/start", {})
    if status != 200 or not pairing.get("device_code") or not pairing.get("user_code"):
        raise LoginError("This Dapier API did not offer device pairing")
    interval = max(int(pairing.get("interval") or 2), 1)
    expires_in = int(pairing.get("expires_in") or 0)
    deadline = time.time() + _wait_seconds(timeout, expires_in)
    print(f"Open:  {base}/device")
    print(f"Code:  {pairing['user_code']}")
    print(f"Waiting for approval (the code is valid for {max(expires_in // 60, 1)} minutes) ...", flush=True)
    while time.time() < deadline:
        status, result = poster(f"{base}/api/agent/device/token", {"device_code": pairing["device_code"]})
        if status == 200 and result.get("status") == "approved":
            if not result.get("token"):
                raise LoginError("The API returned an empty device session")
            session = {
                "token": result["token"],
                "kind": "device",
                "subject": result.get("subject", ""),
                "email": result.get("email", ""),
                "expires_at": int(result.get("expires_at") or 0),
                "obtained_at": int(time.time()),
            }
            config.save_session(session)
            return session
        if status == 200 and result.get("status") == "expired":
            raise LoginError("The pairing code expired; run `dapier auth login` again")
        if result.get("slow_down"):
            interval += 2
        sleeper(interval)
    raise LoginError("Timed out waiting for approval; run `dapier auth login` again")


def revoke_session(api_url, session):
    """Best-effort server-side revocation of a device session."""
    if not session or session.get("kind") != "device":
        return
    try:
        _post_json(f"{api_url.rstrip('/')}/api/agent/device/revoke", {},
                   bearer=session.get("token"), timeout=10)
    except Exception:
        pass


class _CallbackHandler(BaseHTTPRequestHandler):
    result = None

    def do_GET(self):  # noqa: N802 - http.server naming
        query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        _CallbackHandler.result = {
            "code": (query.get("code") or [""])[0],
            "state": (query.get("state") or [""])[0],
            "error": (query.get("error") or [""])[0],
        }
        self.send_response(200)
        self.send_header("content-type", "text/html; charset=utf-8")
        self.send_header("cache-control", "no-store")
        self.end_headers()
        self.wfile.write(
            b"<html><body><p>Signed in. Return to the terminal.</p></body></html>"
        )

    def log_message(self, *args):
        pass


def verify_session_token(id_token, *, jwks_url, issuer, audience, nonce=None):
    import jwt

    key = jwt.PyJWKClient(jwks_url).get_signing_key_from_jwt(id_token)
    claims = jwt.decode(
        id_token, key.key, algorithms=["RS256"], audience=audience,
        issuer=issuer, options={"require": ["exp", "iat", "iss", "aud", "sub"]},
    )
    if nonce is not None and claims.get("nonce") != nonce:
        raise LoginError("Identity token nonce mismatch")
    return claims


def login(api_url, *, timeout=180, opener=None):
    """Run the browser login and store the DTC session. Returns the session."""
    conf = server_config(api_url)
    state, nonce = _random(32), _random(32)
    verifier, challenge = build_pkce()

    try:
        server = HTTPServer((LOOPBACK_HOST, LOOPBACK_PORT), _CallbackHandler)
    except OSError:
        raise LoginError(
            f"Callback port {LOOPBACK_PORT} is busy; free it and run `dapier auth login` again"
        ) from None
    url = authorize_url(conf["auth_base_url"], conf["cli_client_id"], REDIRECT_URI, state, nonce, challenge)
    (opener or webbrowser.open)(url)
    print("Opened the browser for sign-in. Waiting for the callback ...")

    _CallbackHandler.result = None
    thread = threading.Thread(target=server.handle_request, daemon=True)
    thread.start()
    thread.join(timeout)
    server.server_close()
    result = _CallbackHandler.result
    if not result or not result["code"] or result["state"] != state:
        raise LoginError("Sign-in did not complete; run `dapier auth login` again")
    status, raw = _post_form(f"{conf['auth_base_url'].rstrip('/')}/oauth2/token", {
        "grant_type": "authorization_code",
        "client_id": conf["cli_client_id"],
        "code": result["code"],
        "redirect_uri": REDIRECT_URI,
        "code_verifier": verifier,
    })
    if status >= 300:
        raise LoginError("Sign-in failed at the token endpoint")
    data = json.loads(raw.decode() or "{}")
    if "id_token" not in data:
        raise LoginError("Sign-in response did not include an identity token")
    claims = verify_session_token(
        data["id_token"], jwks_url=conf["jwks_url"], issuer=conf["issuer"],
        audience=conf["cli_client_id"], nonce=nonce,
    )
    session = {
        "id_token": data["id_token"],
        "refresh_token": data.get("refresh_token"),
        "subject": claims["sub"],
        "email": claims.get("email"),
        "expires_at": int(claims.get("exp", 0)),
        "obtained_at": int(time.time()),
    }
    config.save_session(session)
    return session


def refresh_session(api_url, session):
    """Refresh the session in place. Returns the session or None."""
    if not session:
        return None
    if session.get("kind") == "device":
        status, data = _post_json(
            f"{api_url.rstrip('/')}/api/agent/device/refresh", {},
            bearer=session.get("token"),
        )
        if status != 200 or not data.get("token"):
            return None
        session.update({
            "token": data["token"],
            "expires_at": int(data.get("expires_at") or 0),
            "obtained_at": int(time.time()),
        })
        config.save_session(session)
        return session
    if not session.get("refresh_token"):
        return None
    try:
        conf = server_config(api_url)
    except LoginError:
        return None
    try:
        status, raw = _post_form(f"{conf['auth_base_url'].rstrip('/')}/oauth2/token", {
            "grant_type": "refresh_token",
            "refresh_token": session["refresh_token"],
            "client_id": conf["cli_client_id"],
        })
        if status >= 300:
            return None
        data = json.loads(raw.decode() or "{}")
        if "id_token" not in data:
            return None
        claims = verify_session_token(
            data["id_token"], jwks_url=conf["jwks_url"], issuer=conf["issuer"],
            audience=conf["cli_client_id"],
        )
    except Exception:
        return None
    session.update({
        "id_token": data["id_token"],
        "refresh_token": data.get("refresh_token") or session.get("refresh_token"),
        "subject": claims["sub"],
        "email": claims.get("email"),
        "expires_at": int(claims.get("exp", 0)),
        "obtained_at": int(time.time()),
    })
    config.save_session(session)
    return session


def describe(session):
    """Session status for display (never includes tokens)."""
    if not session:
        return {"signed_in": False}
    return {
        "signed_in": True,
        "email": session.get("email"),
        "subject": session.get("subject"),
        "expired": int(session.get("expires_at", 0)) <= int(time.time()),
    }
