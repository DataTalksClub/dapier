"""Sync-response webhook triggers: response-mode validation on save, the
challenge handshake, and the router's inline sync run (real outcome, real
failure and suspension semantics)."""
import json

import pytest

from src.dapier.api import router as ingress
from src.dapier.engine.logic import RunSuspended
from src.dapier.triggers import hook_triggers


def _hook_stub(hook_id="orders", kind="webhook", token="tok-123", response=None,
               dedupe_path=None):
    item = {"hook_id": hook_id, "kind": kind, "url": f"u-{hook_id}", "token": token,
            "actions": [], "enabled": True, "dedupe_path": dedupe_path or ""}
    if response is not None:
        item["response"] = response
    return type("T", (), {
        "scan": lambda self, Limit=200: {"Items": [dict(item)]},
        "get_item": lambda self, Key: {"Item": dict(item)} if Key["hook_id"] == hook_id else {},
        "put_item": lambda self, Item: None,
        "delete_item": lambda self, Key: None,
    })()


def _post(path, body, headers=None, query=None):
    response = ingress.handler({
        "requestContext": {"http": {"method": "POST", "path": path}},
        "headers": headers or {},
        "queryStringParameters": query,
        "body": body.decode(),
    }, None)
    parsed = response["body"]
    if response["headers"]["content-type"].startswith("application/json"):
        parsed = json.loads(parsed)
    return response["statusCode"], parsed, response["headers"]["content-type"]


def _auth():
    return {"authorization": "Bearer tok-123", "content-type": "application/json"}


@pytest.fixture
def env(monkeypatch):
    """Router environment: a swappable hook row, a spied queue publish, and
    a spy seam where ``worker.execute`` would run. Tests point
    ``state["sync"]`` at their behavior (return matched ids, or raise)."""
    state = {"stub": {}, "published": [], "parked": [], "notified": [],
             "execute_calls": [], "hooks": None,
             "sync": lambda event, **kwargs: ["webhook-trigger-orders"]}

    def publish(connector, event_type, data, source=None, event_id=None):
        state["published"].append({"connector": connector, "data": data, "id": event_id})

    def execute(event, **kwargs):
        state["execute_calls"].append(event)
        state["hooks"] = kwargs
        return state["sync"](event, **kwargs)

    def park_suspension(susp):
        state["parked"].append(susp)

    def notify_failure(exc, event):
        state["notified"].append((exc, event))

    def hook_table(*args, **kwargs):
        return _hook_stub(**state["stub"])

    def stub_attempt_hooks(attempt):
        # Router-level tests stub the production hooks (they write to
        # EXECUTIONS_TABLE); the wrappers around them stay real.
        return {"before_action": lambda *a, **k: True,
                "after_action": lambda *a, **k: None,
                "on_action_error": lambda *a, **k: None}

    monkeypatch.setenv("EVENT_QUEUE_URL", "https://sqs.example.test/events")
    monkeypatch.setenv("HOOK_TRIGGERS_TABLE", "hooks")
    monkeypatch.setattr(ingress, "_publish", publish)
    monkeypatch.setattr("src.dapier.engine.matching.workflows", lambda: [{
        "id": "webhook-trigger-orders", "enabled": True,
        "trigger": {"connector": "webhook", "event": "request.received",
                    "filters": {"hook": {"equals": "orders"}}},
        "actions": []}])
    monkeypatch.setattr("src.dapier.engine.worker.execute", execute)
    monkeypatch.setattr("src.dapier.engine.worker._attempt_hooks", stub_attempt_hooks)
    monkeypatch.setattr("src.dapier.engine.worker._park_suspension", park_suspension)
    monkeypatch.setattr("src.dapier.engine.worker.notify_failure", notify_failure)
    monkeypatch.setattr("src.dapier.triggers.hook_triggers.get_table", hook_table)
    return state


# --- save-time validation (shared by both dispatchers through build_item) ---

def test_response_defaults_to_ack():
    assert hook_triggers.validate_response(None, "webhook") == {"mode": "ack"}
    assert hook_triggers.validate_response({"mode": "ack"}, "webhook") == {"mode": "ack"}
    assert hook_triggers.validate_response({}, "webhook") == {"mode": "ack"}


def test_response_modes_and_template():
    assert hook_triggers.validate_response({"mode": "challenge"}, "webhook") == {"mode": "challenge"}
    sync = hook_triggers.validate_response(
        {"mode": "sync", "template": {"ok": "{trigger.body.x}"}}, "webhook")
    assert sync == {"mode": "sync", "template": {"ok": "{trigger.body.x}"}}
    assert hook_triggers.validate_response({"mode": "sync", "template": None},
                                           "webhook") == {"mode": "sync"}
    with pytest.raises(hook_triggers.TriggerError):
        hook_triggers.validate_response({"mode": "nope"}, "webhook")
    with pytest.raises(hook_triggers.TriggerError):
        hook_triggers.validate_response({"mode": "sync", "bogus": 1}, "webhook")
    with pytest.raises(hook_triggers.TriggerError):
        hook_triggers.validate_response({"mode": "sync", "template": object()}, "webhook")
    with pytest.raises(hook_triggers.TriggerError):
        hook_triggers.validate_response("sync", "webhook")


def test_response_is_webhook_only():
    with pytest.raises(hook_triggers.TriggerError):
        hook_triggers.validate_response({"mode": "sync"}, "telegram")


def test_saved_item_and_public_view_carry_response():
    table_items = []
    table = type("T", (), {
        "put_item": lambda self, Item: table_items.append(dict(Item)),
        "get_item": lambda self, Key: {"Item": dict(table_items[-1])} if table_items else {},
        "scan": lambda self, Limit=200: {"Items": [dict(i) for i in table_items]},
    })()
    status, body = hook_triggers.api_save(
        {"name": "orders", "actions": [{"type": "webhook", "url": "https://x"}],
         "response": {"mode": "sync", "template": {"hi": "{trigger.body.name}"}}},
        "op", "webhook", table_ref=table)
    assert status == 200
    assert body["response"] == {"mode": "sync", "template": {"hi": "{trigger.body.name}"}}
    status, body = hook_triggers.api_list(table_ref=table)
    assert body["hooks"][0]["response"]["mode"] == "sync"
    # Omitting response on an edit resets to the default, like every field.
    status, body = hook_triggers.api_save(
        {"name": "orders", "actions": [{"type": "webhook", "url": "https://x"}]},
        "op", "webhook", table_ref=table)
    assert body["response"] == {"mode": "ack"}
    with pytest.raises(hook_triggers.TriggerError):
        hook_triggers.api_save(
            {"name": "t", "actions": [{"type": "webhook", "url": "https://x"}],
             "response": {"mode": "sync"}},
            "op", "telegram", table_ref=table)


# --- the ingress: challenge mode ---

def test_challenge_mode_echoes_query_without_publishing(env):
    env["stub"] = {"response": {"mode": "challenge"}}
    status, body, content_type = _post("/hooks/webhook/orders", b"{}",
                                       headers=_auth(), query={"challenge": "ch-1"})
    assert (status, body, content_type) == (200, "ch-1", "text/plain")
    assert env["published"] == [] and env["execute_calls"] == []


def test_challenge_mode_falls_back_to_the_body(env):
    env["stub"] = {"response": {"mode": "challenge"}}
    body = json.dumps({"type": "url_verification", "challenge": "slk-7"}).encode()
    status, text, content_type = _post("/hooks/webhook/orders", body, headers=_auth())
    assert (status, text, content_type) == (200, "slk-7", "text/plain")


def test_challenge_mode_without_a_value_answers_400(env):
    env["stub"] = {"response": {"mode": "challenge"}}
    status, text, _ = _post("/hooks/webhook/orders", b"{}", headers=_auth())
    assert (status, text) == (400, "missing challenge")


# --- the ingress: sync mode ---

def test_sync_mode_runs_inline_and_answers_the_outcome(env):
    env["stub"] = {"response": {"mode": "sync"}}

    def run(event, before_action=None, after_action=None, on_action_error=None):
        after_action("webhook-trigger-orders", "notify", event,
                     output={"id": "m-1"}, duration_ms=5, status="completed")
        return ["webhook-trigger-orders"]

    env["sync"] = run
    status, body, _ = _post("/hooks/webhook/orders",
                            json.dumps({"name": "widget"}).encode(), headers=_auth())
    assert status == 200
    assert body["ok"] is True
    assert body["workflow"] == "webhook-trigger-orders"
    assert body["steps"]["notify"] == {"status": "completed", "output": {"id": "m-1"}}
    event = env["execute_calls"][0]
    assert event["connector"] == "webhook" and event["data"]["hook"] == "orders"
    assert event["data"]["body"] == {"name": "widget"}
    # The production attempt hooks ride along: history, leases, usage.
    assert callable(env["hooks"]["before_action"])
    assert env["published"] == []  # no queue hop


def test_sync_mode_renders_the_template_with_trigger_and_steps(env):
    env["stub"] = {"response": {"mode": "sync", "template": {
        "got": "{trigger.body.name}",
        "count": "{steps.notify.output.id}",
        "kept": 7,
        "nested": ["{trigger.body.name}"]}}}

    def run(event, before_action=None, after_action=None, on_action_error=None):
        after_action("webhook-trigger-orders", "notify", event,
                     output={"id": "m-1"}, duration_ms=5, status="completed")
        return ["webhook-trigger-orders"]

    env["sync"] = run
    status, body, _ = _post("/hooks/webhook/orders",
                            json.dumps({"name": "widget"}).encode(), headers=_auth())
    assert (status, body) == (200, {"got": "widget", "count": "m-1", "kept": 7,
                                    "nested": ["widget"]})


def test_sync_mode_dedupes_a_retried_delivery(env, monkeypatch):
    env["stub"] = {"response": {"mode": "sync"}, "dedupe_path": "event.id"}
    fresh = {"value": False}
    monkeypatch.setattr("src.dapier.triggers.seen.claim",
                        lambda scope, key, **kwargs: fresh["value"])

    body = json.dumps({"event": {"id": "d-1"}}).encode()
    # An already-seen delivery answers without running.
    status, body_json, _ = _post("/hooks/webhook/orders", body, headers=_auth())
    assert status == 200
    assert body_json["duplicate"] is True
    assert body_json["event_id"] == hook_triggers.dedupe_event_id("orders", "d-1")
    assert env["execute_calls"] == []
    # A fresh claim runs under the stable id.
    fresh["value"] = True
    status, body_json, _ = _post("/hooks/webhook/orders", body, headers=_auth())
    assert status == 200 and body_json["ok"] is True
    assert env["execute_calls"][0]["id"] == body_json["event_id"]


def test_sync_mode_failure_releases_and_answers_500(env, monkeypatch):
    env["stub"] = {"response": {"mode": "sync"}, "dedupe_path": "event.id"}
    forgotten = []
    monkeypatch.setattr("src.dapier.triggers.seen.claim", lambda scope, key, **k: True)
    monkeypatch.setattr("src.dapier.triggers.seen.forget",
                        lambda scope, key, **k: forgotten.append(key))

    def boom(event, **kwargs):
        raise RuntimeError("sheets quota")

    env["sync"] = boom
    status, body, _ = _post("/hooks/webhook/orders",
                            json.dumps({"event": {"id": "d-2"}}).encode(), headers=_auth())
    assert status == 500
    assert body["ok"] is False and "sheets quota" in body["error"]
    assert forgotten == [hook_triggers.dedupe_event_id("orders", "d-2")]
    assert env["notified"]  # the failure notified like the worker path


def test_sync_mode_suspension_parks_and_answers_202(env):
    env["stub"] = {"response": {"mode": "sync"}}

    def park(event, **kwargs):
        raise RunSuspended(1893456000.0, {"resume_at": "2030-01-01T00:00:00+00:00"})

    env["sync"] = park
    status, body, _ = _post("/hooks/webhook/orders", b"{}", headers=_auth())
    assert status == 202
    assert body["ok"] is True and body["suspended"] is True
    assert body["resume_at"] == "2030-01-01T00:00:00+00:00"
    assert len(env["parked"]) == 1
    assert env["notified"] == []


def test_ack_mode_still_publishes_202(env):
    env["stub"] = {"response": {"mode": "ack"}}
    status, body, _ = _post("/hooks/webhook/orders", b"{}", headers=_auth())
    assert (status, body) == (202, {"accepted": True})
    assert env["published"] and env["execute_calls"] == []


# --- CLI: the --sync-response flag rides the same API body (parity) ---

def test_cli_hooks_save_sync_response_flag(monkeypatch, tmp_path):
    from dapier_cli import commands

    seen = {}

    def fake_call(api_url, method, path, body=None, **kwargs):
        seen.update(method=method, path=path, body=body)
        return {"created": True, "hook_id": "orders", "kind": "webhook",
                "url": "https://h.test/orders", "token": "tok",
                "header": "authorization", "response": body.get("response")}

    monkeypatch.setattr(commands.api, "call", fake_call)
    hook_file = tmp_path / "hook.json"
    hook_file.write_text(json.dumps(
        {"kind": "webhook", "name": "orders", "actions": []}))

    assert commands.hooks_save("https://api.example.test", str(hook_file),
                               sync_response=True) == 0
    assert (seen["method"], seen["path"]) == ("PUT", "/api/agent/hook-triggers")
    assert seen["body"]["response"] == {"mode": "sync"}

    # Without the flag the body is exactly the file's (no response injected).
    seen.clear()
    assert commands.hooks_save("https://api.example.test", str(hook_file)) == 0
    assert "response" not in seen["body"]

    # A template in the file's own response survives the flag.
    seen.clear()
    hook_file.write_text(json.dumps(
        {"kind": "webhook", "name": "orders", "actions": [],
         "response": {"mode": "ack", "template": {"ok": True}}}))
    assert commands.hooks_save("https://api.example.test", str(hook_file),
                               sync_response=True) == 0
    assert seen["body"]["response"] == {"mode": "sync", "template": {"ok": True}}

