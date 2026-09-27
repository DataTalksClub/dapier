"""webhook action: POST the event — or a templated JSON payload — optionally
HMAC-signed.

Like ``http_request``, the response body lands in the step output (parsed
JSON, or a text preview when it is not JSON), so a signed webhook's chain can
react to what the receiver answered: ``{steps.<id>.output.body.<path>}``.
"""
import hashlib
import hmac
import json
import urllib.request

from . import base
from .templating import render


def run_webhook(action, event, *, steps=None, transport=None):
    body = _request_body(action, event, steps)
    headers = {"content-type": "application/json", "user-agent": "dapier/0.1"}
    if action.get("secret_id"):
        digest = hmac.new(base._signing_secret(action["secret_id"]).encode(), body, hashlib.sha256).hexdigest()
        headers["x-dapier-signature"] = f"sha256={digest}"
    timeout = action.get("timeout_seconds", 10)
    if transport is not None:
        status, raw = transport("POST", action["url"], headers=headers, body=body, timeout=timeout)
    else:
        request = urllib.request.Request(action["url"], data=body, headers=headers, method="POST")
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read()
            status = response.status
    if status >= 300:
        raise RuntimeError(f"webhook returned HTTP {status}")
    return {"status": status, "body": _decode_body(raw)}


def _request_body(action, event, steps):
    """The bytes to POST: the rendered ``payload`` when set, else the whole event.

    A payload that does not render to JSON is a clear step failure, in the
    same dialect as the auth errors — never a silently mangled body.
    """
    payload = str(action.get("payload") or "").strip()
    if not payload:
        return json.dumps(event, separators=(",", ":"), sort_keys=True).encode()
    rendered = render(payload, event, steps)
    try:
        parsed = json.loads(rendered)
    except json.JSONDecodeError as exc:
        raise ValueError(f"webhook payload rendered to invalid JSON: {exc}") from exc
    return json.dumps(parsed, separators=(",", ":"), sort_keys=True).encode()


def _decode_body(raw, limit=4000):
    """Parsed JSON body, else a text preview — the http_request behavior."""
    try:
        text = raw.decode()
    except (AttributeError, UnicodeDecodeError):
        return str(raw)[:limit]
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return text[:limit]
