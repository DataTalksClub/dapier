"""HTTPS client for the Dapier agent API.

Sends the DTC identity as a Bearer token, retries once after a DTC session
refresh on 401, and never writes tokens to logs, debug output, or errors.
"""

import json
import urllib.error
import urllib.request

from . import auth, config


class ApiError(Exception):
    def __init__(self, message, status=None):
        super().__init__(message)
        self.status = status


def _request(api_url, method, path, session, body=None, timeout=20, debug=False):
    payload = json.dumps(body).encode() if body is not None else None
    bearer = session_bearer(session)
    request = urllib.request.Request(
        f"{api_url}{path}", data=payload, method=method,
        headers={
            "content-type": "application/json",
            "authorization": f"Bearer {bearer}",
        },
    )
    if debug:
        print(f"{method} {path}")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read()
            status = response.status
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        status = exc.code
    if debug:
        print(f"-> HTTP {status}")
    try:
        data = json.loads(raw.decode() or "{}")
    except ValueError:
        data = {}
    return status, (data if isinstance(data, dict) else {})


def session_bearer(session):
    """The bearer value for a stored session, whichever kind it is."""
    return (session.get("token") or session.get("id_token") or "").strip()


def call(api_url, method, path, body=None, timeout=20, debug=False):
    """Call the agent API, refreshing the session once on 401."""
    session = config.load_session()
    if not session or not session_bearer(session):
        raise ApiError("Not signed in; run `dapier auth login`", status=401)
    status, data = _request(api_url, method, path, session, body, timeout, debug)
    if status == 401 and auth.refresh_session(api_url, session):
        status, data = _request(api_url, method, path, session, body, timeout, debug)
    if status == 401:
        raise ApiError("Not signed in; run `dapier auth login`", status=401)
    if status == 403:
        raise ApiError(data.get("error") or "Denied", status=403)
    if status == 404:
        raise ApiError(data.get("error") or "Not found", status=404)
    if status == 429:
        raise ApiError(data.get("error") or "Rate-limited; retry shortly", status=429)
    if status >= 300:
        raise ApiError(data.get("error") or f"Request failed (HTTP {status})", status=status)
    return data
