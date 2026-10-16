#!/usr/bin/env python3
"""Fetch a short-lived Dapier provider token into a local variable.

Stdlib only. The script calls the same two endpoints `dapier token exec`
used — resolve the human reference, then mint the token — and returns the
token as a plain string. Nothing is printed, written to disk, or placed in
the environment; keep it in a local and `del` it when done:

    import dtoken
    access_token = dtoken.dapier_token("gmail datatalks", "admin-os")
    # ... Authorization: Bearer {access_token} ...
    del access_token

Auth is the DTC session in ~/.config/dapier/session.json (via
`dapier auth login`); the API base is $DAPIER_API_URL, then api_url in
~/.config/dapier/config.json, then the production default.
"""

import json
import os
import urllib.error
import urllib.parse
import urllib.request

DEFAULT_API_URL = "https://dapier.dtcdev.click"


class DapierError(Exception):
    pass


def _config_dir():
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    return os.path.join(base, "dapier")


def api_base_url(explicit=None):
    """The Dapier API origin: explicit > $DAPIER_API_URL > config file > default."""
    if explicit:
        return explicit.rstrip("/")
    env = (os.environ.get("DAPIER_API_URL") or "").strip()
    if env:
        return env.rstrip("/")
    try:
        with open(os.path.join(_config_dir(), "config.json"), encoding="utf-8") as handle:
            saved = (json.load(handle).get("api_url") or "").strip()
    except (OSError, ValueError, AttributeError):
        saved = ""
    return (saved or DEFAULT_API_URL).rstrip("/")


def _bearer():
    try:
        with open(os.path.join(_config_dir(), "session.json"), encoding="utf-8") as handle:
            session = json.load(handle)
    except (OSError, ValueError):
        raise DapierError("Not signed in; run `dapier auth login`") from None
    token = ((session or {}).get("token") or (session or {}).get("id_token") or "").strip()
    if not token:
        raise DapierError("Not signed in; run `dapier auth login`")
    return token


def _call(api, bearer, method, path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        api + path, data=data, method=method,
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {bearer}"},
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            payload = json.loads(resp.read().decode() or "{}")
    except urllib.error.HTTPError as exc:
        try:
            payload = json.loads(exc.read().decode() or "{}")
        except ValueError:
            payload = {}
        detail = str((payload or {}).get("error") or f"HTTP {exc.code}")
        raise DapierError(f"Dapier {method} {path} failed: {detail}") from None
    return payload if isinstance(payload, dict) else {}


def resolve_connection(ref, agent, *, api_url=None):
    """The internal connection id a human reference names."""
    api = api_base_url(api_url)
    bearer = _bearer()
    params = urllib.parse.urlencode({"ref": ref, "agent": agent})
    view = _call(api, bearer, "GET", f"/api/agent/connections/resolve?{params}")
    connection_id = str(view.get("connection_id") or "").strip()
    if not connection_id:
        raise DapierError(f"Could not resolve connection {ref!r}")
    return connection_id


def dapier_token(ref, agent, *, api_url=None):
    """A fresh provider access token for ``ref`` (e.g. "gmail datatalks").

    Mirrors the CLI's binding check: the connection must carry a verified
    provider account, and the minted token's account must match it —
    otherwise no token is returned. Raises DapierError (grant, consent,
    login, rate-limit) without ever exposing the token itself.
    """
    api = api_base_url(api_url)
    bearer = _bearer()
    connection_id = resolve_connection(ref, agent, api_url=api)
    view = _call(api, bearer, "GET",
                 f"/api/agent/connections/{urllib.parse.quote(connection_id, safe='')}?"
                 + urllib.parse.urlencode({"agent": agent}))
    expected = {v for v in (view.get("expected_account_id"), view.get("verified_account_id")) if v}
    if not expected:
        raise DapierError(
            f"Connection {connection_id} has no verified provider account; "
            "ask an operator to connect it first")
    token = _call(api, bearer, "POST", "/api/agent/token",
                  {"connection_id": connection_id, "agent": agent})
    if token.get("provider_account_id") not in expected:
        raise DapierError(
            f"Provider account {token.get('provider_account_id')} does not match "
            f"connection {connection_id}; refusing the token")
    access_token = str(token.get("access_token") or "")
    if not access_token:
        raise DapierError("Dapier returned no access token")
    return access_token
