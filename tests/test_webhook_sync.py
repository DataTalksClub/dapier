"""Webhook sync-response: the parity and suspension edges the other
sync-response suites leave open.

Complements tests/test_hook_sync_response.py (the router contract over
stubbed worker seams) and tests/test_sync_response.py (the guarantees):
here the save-time ``response`` validation is proven on BOTH operator
routes (the admin session route and the agent bearer route share
``api_save``, so a malformed config is a 400 on each), ``dapier hooks show``
prints the stored config, and a delay step past the inline cap really parks
its continuation on the event queue from inside the HTTP request — the run
resumes out of band while the caller gets the documented 202.
"""
import json
import os
import time
from unittest.mock import patch

import boto3
import pytest

from src.dapier.api import agent as agent_api
from src.dapier.api import router as ingress
from src.dapier.triggers import hook_triggers


class _StubHookTable:
    def __init__(self):
        self.items = {}
        self.puts = []

    def put_item(self, Item, **_kwargs):
        self.puts.append(dict(Item))
        self.items[Item["hook_id"]] = dict(Item)

    def get_item(self, Key, **_kwargs):
        item = self.items.get(Key["hook_id"])
        return {"Item": dict(item)} if item else {}

    def scan(self, Limit=200):
        return {"Items": [dict(item) for item in self.items.values()]}

    def delete_item(self, Key, **_kwargs):
        self.items.pop(Key["hook_id"], None)


class _ExecTable:
    """Captures the step writes the inline run makes against EXECUTIONS_TABLE."""

    def __init__(self):
        self.items = {}
        self.updates = []

    def put_item(self, Item, **_kwargs):
        self.items[Item["execution_id"]] = dict(Item)

    def update_item(self, Key, ExpressionAttributeValues=None, **_kwargs):
        values = ExpressionAttributeValues or {}
        self.updates.append((Key["execution_id"], dict(values)))
        item = self.items.setdefault(Key["execution_id"], {})
        for placeholder, field in (":status", "status"), (":output", "output"), (":error", "error"):
            if placeholder in values:
                item[field] = values[placeholder]

    def get_item(self, Key, **_kwargs):
        item = self.items.get(Key["execution_id"])
        return {"Item": dict(item)} if item else {}

    def query(self, **_kwargs):
        return {"Items": []}


def _save_body(response):
    return {"name": "orders", "kind": "webhook",
            "actions": [{"type": "webhook", "url": "https://x.test/hook"}],
            "response": response}


# --- save-time validation on both operator routes ----------------------------

def test_agent_route_rejects_a_malformed_response(monkeypatch):
    stub = _StubHookTable()
    monkeypatch.setenv("HOOK_TRIGGERS_TABLE", "hooks")
    monkeypatch.setenv("CONNECTIONS_TABLE", "connections")
    monkeypatch.setenv("GRANTS_TABLE", "grants")
    monkeypatch.setattr("src.dapier.api.agent.authenticate", lambda event: ("sub-1", None))
    monkeypatch.setattr("src.dapier.api.agent.authz.is_operator", lambda payload: True)
    monkeypatch.setattr("src.dapier.triggers.hook_triggers.get_table", lambda *a, **k: stub)
    for bad in ({"mode": "bogus"}, {"mode": "sync", "status": 999}, "sync"):
        event = {"headers": {}, "body": json.dumps(_save_body(bad))}
        response = agent_api.hook_triggers_api(event, "PUT")
        assert response["statusCode"] == 400, bad
        assert "response" in json.loads(response["body"])["error"]
    assert stub.puts == []  # nothing was saved


def test_admin_route_rejects_a_malformed_response(monkeypatch):
    from src.dapier.api import admin as admin_api
    from src.dapier.auth import session

    stub = _StubHookTable()
    monkeypatch.setenv("HOOK_TRIGGERS_TABLE", "hooks")
    monkeypatch.setattr(session, "_credentials",
                        lambda: {"username": "admin", "password": "pw"})
    monkeypatch.setattr("src.dapier.triggers.hook_triggers.get_table", lambda *a, **k: stub)
    cookie = session._sign({"sub": "op@datatalks.club", "subject": "op-sub",
                            "exp": int(time.time()) + 600})
    event = {
        "requestContext": {"http": {"method": "PUT", "path": "/api/admin/hook-triggers"}},
        "headers": {"host": "dapier.example.test", "origin": "https://dapier.example.test"},
        "cookies": [f"dapier_session={cookie}"],
        "body": json.dumps(_save_body({"mode": "nope"})),
    }
    response = admin_api.route(event, "PUT", "/api/admin/hook-triggers")
    assert response["statusCode"] == 400
    assert "response" in json.loads(response["body"])["error"]
    assert stub.puts == []  # nothing was saved

    # The valid config saves through the same route.
    event["body"] = json.dumps(_save_body({"mode": "sync", "status": 201,
                                           "template": {"ok": True}}))
    ok = admin_api.route(event, "PUT", "/api/admin/hook-triggers")
    assert ok["statusCode"] == 200
    assert json.loads(ok["body"])["response"] == {
        "mode": "sync", "status": 201, "template": {"ok": True}}


# --- the CLI prints the stored config ----------------------------------------

def test_cli_hooks_show_prints_the_response(monkeypatch, capsys):
    from dapier_cli import commands

    def fake_call(api_url, method, path, body=None, **kwargs):
        return {"hooks": [{"hook_id": "orders", "kind": "webhook",
                           "url": "https://h.test/orders", "token": "tok",
                           "header": "authorization", "enabled": True,
                           "response": {"mode": "sync", "status": 201}}]}

    monkeypatch.setattr(commands.api, "call", fake_call)
    assert commands.hooks_show("https://api.example.test", "orders") == 0
    out, _ = capsys.readouterr()
    assert 'response: {"mode": "sync", "status": 201}' in out


# --- a real delay step parks the run out of band ------------------------------

@pytest.fixture
def sync_engine(monkeypatch, tmp_path):
    """The real engine, no worker seams stubbed, with a delay-step hook: the
    suspension parks its continuation through the worker's own queue seam."""
    monkeypatch.setenv("EVENT_QUEUE_URL", "https://sqs.example.test/events")
    monkeypatch.setenv("HOOK_TRIGGERS_TABLE", "hooks")
    monkeypatch.setenv("EXECUTIONS_TABLE", "executions")
    monkeypatch.setenv("WORKFLOWS_DIR", str(tmp_path))  # empty: only the hook's workflow
    for unused in ("TASK_USAGE_TABLE", "PUBLISHED_WORKFLOWS_TABLE",
                   "EMAIL_TRIGGERS_TABLE", "SCHEDULE_TRIGGERS_TABLE",
                   "POLL_TRIGGERS_TABLE", "TRIGGER_INBOX_TABLE"):
        monkeypatch.delenv(unused, raising=False)
    state = {"published": [], "sent": [], "table": _ExecTable(), "notified": []}

    def publish(connector, event_type, data, source=None, event_id=None):
        state["published"].append({"connector": connector, "data": data, "id": event_id})

    def send_message(**kwargs):
        state["sent"].append(kwargs)

    class Dynamo:
        def Table(self, _name):
            return state["table"]

    class FakeSqs:
        def send_message(self, **kwargs):
            send_message(**kwargs)

    def worker_sqs():
        return FakeSqs()

    hook_table = type("T", (), {
        "scan": lambda self, Limit=200: {"Items": [dict({
            "hook_id": "orders", "kind": "webhook", "url": "u-orders",
            "token": "tok-123", "enabled": True, "dedupe_path": "",
            "response": {"mode": "sync"},
            "actions": [{"id": "pause", "type": "delay", "seconds": 90}],
        })]},
        "get_item": lambda self, Key: {"Item": {
            "hook_id": "orders", "kind": "webhook", "url": "u-orders",
            "token": "tok-123", "enabled": True, "dedupe_path": "",
            "response": {"mode": "sync"},
            "actions": [{"id": "pause", "type": "delay", "seconds": 90}],
        }} if Key["hook_id"] == "orders" else {},
        "put_item": lambda self, Item: None,
        "delete_item": lambda self, Key: None,
    })()

    monkeypatch.setattr(boto3, "resource", lambda service: Dynamo())
    monkeypatch.setattr(ingress, "_publish", publish)
    monkeypatch.setattr(ingress.queue, "send_message", send_message)
    monkeypatch.setattr("src.dapier.engine.worker._sqs", worker_sqs)
    monkeypatch.setattr("src.dapier.engine.worker.notify_failure",
                        lambda exc, event: state["notified"].append(exc))
    monkeypatch.setattr("src.dapier.triggers.seen.claim", lambda scope, key, **k: True)
    monkeypatch.setattr("src.dapier.triggers.hook_triggers.get_table",
                        lambda *a, **k: hook_table)
    return state


def test_sync_delay_suspension_parks_the_run_and_answers_202(sync_engine):
    """A delay past the inline cap cannot sleep inside the request: the run
    suspends, the worker's own park path leaves the continuation on the event
    queue, and the caller gets the documented 202 — nothing hangs."""
    started = time.monotonic()
    response = ingress.handler({
        "requestContext": {"http": {"method": "POST", "path": "/hooks/webhook/orders"}},
        "headers": {"authorization": "Bearer tok-123",
                    "content-type": "application/json"},
        "body": "{}",
    }, None)
    assert time.monotonic() - started < 5  # no in-request sleeping

    assert response["statusCode"] == 202
    body = json.loads(response["body"])
    assert body["ok"] is True and body["suspended"] is True
    assert body["resume_at"]  # the moment the run continues, from the delay step
    # The continuation left through the worker's own queue seam, not the
    # ingress publish: one parked envelope, nothing published.
    assert sync_engine["published"] == []
    assert len(sync_engine["sent"]) == 1
    parked = json.loads(sync_engine["sent"][0]["MessageBody"])["dapier_resume"]
    assert parked["workflow_id"] == "webhook-trigger-orders"
    assert parked["paused_ids"] == ["pause"]
    assert parked["event"]["id"] == body["event_id"]
    assert 1 <= sync_engine["sent"][0]["DelaySeconds"] <= 900
    # Run history shows the pause exactly like a queued run's would.
    pause_id = f"webhook-trigger-orders:pause:{body['event_id']}"
    assert sync_engine["table"].items[pause_id]["status"] == "delayed"
    # A suspension is not a failure: nobody was notified.
    assert sync_engine["notified"] == []
