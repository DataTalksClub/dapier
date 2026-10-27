"""Developer notify webhook: verify shared secret, create Hub intake.

POST ``/hooks/developer`` (Zoom-style shared-secret auth). Prefer Hub intake
create so Developer sees the event; optional best-effort assign wakes a
session when Hub accepts it.

Secrets stay in env / the DataOps team secrets store — never in git:

- ``DEVELOPER_WEBHOOK_SECRET`` or ``DEVELOPER_WEBHOOK_SECRET_NAME``
- ``HUB_INTAKE_URL`` (default ``https://hub.dtcdev.click/api/intake/tasks``)
- ``HUB_INTAKE_TOKEN_SECRET_NAME`` (the DataOps team secrets store; bearer token string or
  JSON ``{"token": "..."}``)
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import urllib.error
import urllib.request
from datetime import datetime, timezone

from ...engine.actions import base

SIGNATURE_HEADER = "x-dapier-signature"
SIGNATURE_PREFIX = "sha256="
DEFAULT_HUB_INTAKE_URL = "https://hub.dtcdev.click/api/intake/tasks"
MAX_TITLE = 200
MAX_TEXT = 8000


def _headers_lower(headers):
    return {str(k).lower(): str(v) for k, v in (headers or {}).items()}


def webhook_secret():
    """Shared secret for inbound Developer deliveries, or empty if unset."""
    raw = (os.environ.get("DEVELOPER_WEBHOOK_SECRET") or "").strip()
    if raw:
        return raw
    secret_id = (os.environ.get("DEVELOPER_WEBHOOK_SECRET_NAME") or "").strip()
    if not secret_id:
        return ""
    value = base.secrets_value(secret_id)
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return value.strip()
    if isinstance(parsed, dict):
        return str(
            parsed.get("webhook_secret")
            or parsed.get("secret")
            or parsed.get("token")
            or ""
        ).strip()
    return str(parsed).strip()


def hub_intake_url():
    return (os.environ.get("HUB_INTAKE_URL") or DEFAULT_HUB_INTAKE_URL).strip().rstrip("/")


def hub_bearer_token():
    secret_id = (os.environ.get("HUB_INTAKE_TOKEN_SECRET_NAME") or "").strip()
    if not secret_id:
        return ""
    value = base.secrets_value(secret_id)
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return value.strip()
    if isinstance(parsed, dict):
        return str(parsed.get("token") or parsed.get("access_token") or "").strip()
    return str(parsed).strip()


def signature_for(secret, body):
    return hmac.new(secret.encode(), body or b"", hashlib.sha256).hexdigest()


def verify(headers, body, secret):
    """Accept HMAC ``X-Dapier-Signature: sha256=<hex>`` or Bearer equal to secret."""
    if not secret:
        return False
    normalized = _headers_lower(headers)
    sig = normalized.get(SIGNATURE_HEADER, "").strip()
    if sig:
        value = sig
        if value.lower().startswith(SIGNATURE_PREFIX):
            value = value[len(SIGNATURE_PREFIX):].strip()
        return hmac.compare_digest(value.lower(), signature_for(secret, body))
    auth = normalized.get("authorization", "").strip()
    if auth.lower().startswith("bearer "):
        token = auth[7:].strip()
        return hmac.compare_digest(token, secret)
    return False


def _clip(value, limit):
    text = str(value if value is not None else "")
    if len(text.encode("utf-8")) <= limit:
        return text
    while text and len(text.encode("utf-8")) > limit:
        text = text[:-1]
    return text


def build_intake_payload(message, *, default_project="dapier"):
    """Shape a Hub ``intake create`` body from a Developer webhook JSON object."""
    if not isinstance(message, dict):
        raise ValueError("body must be a JSON object")
    title = _clip(message.get("title") or "dapier developer event", MAX_TITLE).strip()
    if not title:
        title = "dapier developer event"
    text = message.get("text")
    if text is None:
        text = json.dumps(message, ensure_ascii=False, sort_keys=True)
    text = _clip(text, MAX_TEXT)
    project = str(message.get("project") or default_project).strip() or default_project
    key = str(message.get("key") or "").strip()
    if not key:
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]
        key = f"dapier-developer-{digest}"
    source = message.get("source") if isinstance(message.get("source"), dict) else {}
    source_type = str(source.get("type") or message.get("source_type") or "forwarded").strip() or "forwarded"
    if source_type not in ("chat", "voice", "forwarded", "unknown"):
        source_type = "forwarded"
    reference = source.get("reference") or message.get("source_reference") or message.get("event_id")
    timestamp = source.get("timestamp") or message.get("source_time")
    return {
        "key": key,
        "project": project,
        "title": title,
        "text": text,
        "source": {
            "type": source_type,
            "reference": None if reference in (None, "") else str(reference)[:500],
            "timestamp": None if timestamp in (None, "") else str(timestamp)[:64],
        },
    }


def create_hub_intake(payload, *, transport=None, token=None, url=None):
    """POST Hub intake create; return ``(status, body_dict_or_text)``."""
    token = token if token is not None else hub_bearer_token()
    if not token:
        raise RuntimeError("HUB_INTAKE_TOKEN_SECRET_NAME is not configured")
    endpoint = url or hub_intake_url()
    body = json.dumps(payload, separators=(",", ":")).encode()
    headers = {
        "content-type": "application/json",
        "authorization": f"Bearer {token}",
        "user-agent": "dapier-developer-webhook/1",
    }
    if transport is not None:
        return transport("POST", endpoint, headers=headers, body=body)
    request = urllib.request.Request(endpoint, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            raw = response.read()
            status = response.status
    except urllib.error.HTTPError as exc:
        raw = exc.read() if exc.fp else b""
        status = exc.code
    try:
        parsed = json.loads(raw) if raw else {}
    except (ValueError, UnicodeDecodeError):
        parsed = {"raw": raw.decode("utf-8", errors="replace")[:1000]}
    return status, parsed


def assign_hub_task(task_id, *, expected_version, key, transport=None, token=None, url=None):
    """Best-effort Hub intake assign to wake Developer. Returns status/body."""
    token = token if token is not None else hub_bearer_token()
    if not token or not task_id:
        return None, None
    base_url = (url or hub_intake_url()).rstrip("/")
    endpoint = f"{base_url}/{task_id}/assign"
    payload = {"key": key, "expected_version": int(expected_version)}
    body = json.dumps(payload, separators=(",", ":")).encode()
    headers = {
        "content-type": "application/json",
        "authorization": f"Bearer {token}",
        "user-agent": "dapier-developer-webhook/1",
    }
    if transport is not None:
        return transport("POST", endpoint, headers=headers, body=body)
    request = urllib.request.Request(endpoint, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            raw = response.read()
            status = response.status
    except urllib.error.HTTPError as exc:
        raw = exc.read() if exc.fp else b""
        status = exc.code
    except Exception:
        return None, None
    try:
        parsed = json.loads(raw) if raw else {}
    except (ValueError, UnicodeDecodeError):
        parsed = {}
    return status, parsed


def handle(headers, body, *, transport=None, now=None):
    """Return ``(http_status, json_body)`` for ``POST /hooks/developer``."""
    secret = webhook_secret()
    if not secret:
        return 503, {"error": "developer webhook is not configured"}
    if not verify(headers, body, secret):
        return 401, {"error": "invalid signature"}
    try:
        message = json.loads(body or b"{}")
    except (ValueError, UnicodeDecodeError):
        return 400, {"error": "invalid JSON"}
    try:
        payload = build_intake_payload(message)
    except ValueError as exc:
        return 400, {"error": str(exc)}
    try:
        status, created = create_hub_intake(payload, transport=transport)
    except RuntimeError as exc:
        return 503, {"error": str(exc)}
    if status >= 300:
        return 502, {"error": "hub intake failed", "hub_status": status, "hub": created}
    task = created.get("task") if isinstance(created, dict) else None
    task_id = None
    version = None
    if isinstance(task, dict):
        task_id = task.get("id")
        version = task.get("version")
    elif isinstance(created, dict):
        task_id = created.get("id")
        version = created.get("version")
    wake = message.get("wake", True)
    assign = None
    if wake and task_id and version is not None:
        assign_status, _assign_body = assign_hub_task(
            task_id,
            expected_version=version,
            key=f"{payload['key']}-assign",
            transport=transport,
        )
        if assign_status is not None:
            assign = {"hub_status": assign_status, "ok": assign_status < 300}
    occurred = (now or datetime.now(timezone.utc)).isoformat()
    return 202, {
        "accepted": True,
        "hub_task_id": task_id,
        "intake_key": payload["key"],
        "project": payload["project"],
        "occurred_at": occurred,
        "assign": assign,
    }
