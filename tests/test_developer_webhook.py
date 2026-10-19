"""Developer webhook auth and Hub intake payload shaping."""

import hashlib
import hmac
import json
import os

from src.dapier.triggers.intake import developer_webhook as dw


def _sign(body: bytes, secret: str) -> dict:
    digest = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return {"X-Dapier-Signature": f"sha256={digest}"}


def test_verify_hmac_and_bearer():
    body = b'{"title":"t"}'
    secret = "test-secret"
    assert dw.verify(_sign(body, secret), body, secret)
    assert not dw.verify(_sign(body, "other"), body, secret)
    assert dw.verify({"Authorization": f"Bearer {secret}"}, body, secret)
    assert not dw.verify({"Authorization": "Bearer nope"}, body, secret)
    assert not dw.verify({}, body, secret)


def test_build_intake_payload_defaults(monkeypatch):
    payload = dw.build_intake_payload({"title": "Hello", "text": "Body"})
    assert payload["title"] == "Hello"
    assert payload["text"] == "Body"
    assert payload["project"] == "dapier"
    assert payload["key"].startswith("dapier-developer-")
    assert payload["source"]["type"] == "forwarded"


def test_handle_creates_intake_and_optional_assign(monkeypatch):
    secret = "hook-secret"
    monkeypatch.setenv("DEVELOPER_WEBHOOK_SECRET", secret)
    monkeypatch.setenv("HUB_INTAKE_TOKEN_SECRET_ID", "dapier/hub-intake")
    monkeypatch.setattr(dw.base, "secrets_value", lambda _sid: "hub-token")
    calls = []

    def transport(method, url, headers=None, body=None):
        calls.append((method, url, headers, body))
        if url.endswith("/assign"):
            return 200, {"assigned": True}
        return 200, {"task": {"id": "task-1", "version": 1}}

    body = json.dumps({"title": "ping", "text": "hi", "key": "k1", "wake": True}).encode()
    status, answer = dw.handle(_sign(body, secret), body, transport=transport)
    assert status == 202
    assert answer["hub_task_id"] == "task-1"
    assert answer["intake_key"] == "k1"
    assert answer["assign"]["ok"] is True
    assert len(calls) == 2
    assert calls[0][1].endswith("/api/intake/tasks") or "intake/tasks" in calls[0][1]


def test_handle_rejects_bad_signature(monkeypatch):
    monkeypatch.setenv("DEVELOPER_WEBHOOK_SECRET", "hook-secret")
    status, answer = dw.handle({}, b'{"title":"x"}')
    assert status == 401
    assert answer["error"] == "invalid signature"


def test_handle_unconfigured(monkeypatch):
    monkeypatch.delenv("DEVELOPER_WEBHOOK_SECRET", raising=False)
    monkeypatch.delenv("DEVELOPER_WEBHOOK_SECRET_ID", raising=False)
    status, answer = dw.handle({}, b"{}")
    assert status == 503
