"""webhook action: POST the event — or a templated JSON payload — optionally
HMAC-signed.

Like ``http_request``, the response body lands in the step output (parsed
JSON, or a text preview when it is not JSON), so a signed webhook's chain can
react to what the receiver answered: ``{steps.<id>.output.body.<path>}``.
The response headers ride along lowercased in ``response_headers`` whenever
the transport surfaced them, so a chain can read
``{steps.<id>.output.response_headers.content-type}`` — a legacy transport
that hides them keeps the old two-key output.
"""
import hashlib
import hmac
import json
import urllib.error
import urllib.request

from . import base
from .templating import render


class HttpError(RuntimeError):
    """A webhook/HTTP response at or above HTTP 300.

    Carries the response ``status`` and, when the transport surfaced the
    headers, the server's ``retry_after`` hint in seconds. A plain
    RuntimeError everywhere else — the engine's autoretry is the only
    reader of the extra attributes.
    """

    def __init__(self, message, *, status=None, retry_after=None):
        super().__init__(message)
        self.status = status
        self.retry_after = retry_after


def retry_after_seconds(headers):
    """The ``Retry-After`` header as seconds, or None when absent/unparsable.

    Only the delay-in-seconds form is parsed (the HTTP-date form is left to
    the ordinary backoff); anything unreadable returns None.
    """
    try:
        return float(str(headers.get("retry-after", "")).strip())
    except (AttributeError, TypeError, ValueError):
        return None


def lowercase_headers(source):
    """Response headers as a lowercased plain dict, or None when none surfaced.

    Accepts anything header-ish: an ``http.client.HTTPResponse`` (the urllib
    fallback's response, whose ``headers`` is an ``email.message.Message``)
    or a plain mapping (an upgraded transport's third element). A legacy
    transport that returns no headers at all keeps this action's old
    two-key output shape — the None here is the signal for that.
    """
    headers = getattr(source, "headers", source)
    if headers is None:
        return None
    try:
        items = headers.items()
    except AttributeError:
        return None
    return {str(name).lower(): str(value) for name, value in items}


def transport_response(result):
    """An injected transport's return as ``(status, raw, headers_or_None)``.

    The transport seam returns ``(status, raw)``; a transport may append the
    response headers (any ``lowercase_headers``-able object) as a third
    element, normalized here exactly like the urllib fallback's. Both shapes
    are accepted, so every existing transport — and its tests — keeps
    working.
    """
    if isinstance(result, tuple) and len(result) == 3:
        return result[0], result[1], lowercase_headers(result[2])
    status, raw = result
    return status, raw, None


def request_output(status, raw, headers=None):
    """The webhook/http_request step output: status and decoded body always,
    the lowercased response headers as ``response_headers`` whenever the
    transport surfaced them (present-key only, so a hidden-header transport
    keeps the exact ``{status, body}`` output earlier steps and tests read)."""
    output = {"status": status, "body": _decode_body(raw)}
    if headers:
        output["response_headers"] = headers
    return output


def run_webhook(action, event, *, steps=None, transport=None):
    body = _request_body(action, event, steps)
    headers = {"content-type": "application/json", "user-agent": "dapier/0.1"}
    if action.get("secret_id"):
        digest = hmac.new(base._signing_secret(action["secret_id"]).encode(), body, hashlib.sha256).hexdigest()
        headers["x-dapier-signature"] = f"sha256={digest}"
    timeout = action.get("timeout_seconds", 10)
    if transport is not None:
        status, raw, response_headers = transport_response(
            transport("POST", action["url"], headers=headers, body=body, timeout=timeout))
    else:
        request = urllib.request.Request(action["url"], data=body, headers=headers, method="POST")
        response_headers = None
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                raw = response.read()
                status = response.status
                response_headers = lowercase_headers(response)
        except urllib.error.HTTPError as exc:
            # urllib raises for any HTTP 4xx/5xx before this function's own
            # status check can see it; re-raise with the same status and
            # Retry-After info the direct path attaches.
            raise HttpError(
                f"webhook returned HTTP {exc.code}",
                status=exc.code,
                retry_after=retry_after_seconds(exc.headers),
            ) from exc
    if status >= 300:
        raise HttpError(f"webhook returned HTTP {status}", status=status)
    return request_output(status, raw, response_headers)


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
