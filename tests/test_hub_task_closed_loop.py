"""Tests for workflows/hub-task-closed-loop.yaml (Hub task -> Dev closed-loop webhook)."""
import json
from pathlib import Path
import pytest
import yaml

from src.dapier.api.designer_store.validation import parse_workflow
from src.dapier.connectors.webhook import run_http_request
from src.dapier.engine.actions.code import run_code
from src.dapier.engine.actions.templating import render
from src.dapier.engine.logic_pkg.chains import run_chain

WF_PATH = Path(__file__).parent.parent / "workflows/hub-task-closed-loop.yaml"

USAGE_BODY_BELOW = {
    "account": "grokbot",
    "provider": "grok",
    "remaining_percent": 0.4,
    "threshold_percent": 1,
    "below_threshold": True,
    "fresh": True,
}
USAGE_BODY_ABOVE = {
    "account": "grokbot",
    "provider": "grok",
    "remaining_percent": 42.0,
    "threshold_percent": 1,
    "below_threshold": False,
    "fresh": True,
}
FULL_STORAGE = {
    "webhook_url": "https://bot.example.invalid/hook",
    "webhook_token": "bot-token",
    "hub_usage_url": "https://hub.dtcdev.click/api/quota/grokbot",
    "hub_messages_url": "https://hub.dtcdev.click/api/messages",
    "hub_api_token": "hub-token",
    "hub_recipient": "agent:dev",
    "hub_refresh_url": "https://hub.dtcdev.click/api/token/refresh",
    "hub_refresh_token": "hub-refresh-token",
}


def _load():
    return yaml.safe_load(WF_PATH.read_text())


def _run_chain(storage, body_payload, usage_transport):
    wf = _load()
    store = dict(storage)
    calls = []

    def transport(method, url, headers=None, body=None, timeout=None):
        calls.append({"method": method, "url": url, "headers": headers or {}, "body": body})
        return usage_transport(method, url, headers, body)

    def run_action(step, event, workflow_id, steps=None):
        kind = step.get("type")
        if kind == "storage_get":
            key = step.get("key")
            return {"key": key, "value": store.get(key, ""), "found": key in store}
        if kind == "storage_set":
            key = step.get("key")
            store[key] = render(step.get("value"), event, steps)
            return {"key": key, "stored": True}
        if kind == "code":
            return run_code(step, event, steps=steps)
        if kind == "http_request":
            return run_http_request(step, event, steps=steps, transport=transport)
        raise AssertionError(f"unexpected step type {kind}")

    event = {
        "connector": "webhook",
        "data": {
            "hook": "hub-task-status",
            "content_type": "application/json",
            "body": body_payload,
            "query": {},
        },
    }
    step_outputs = {}
    stop = run_chain("hub-task-closed-loop", wf["actions"], event, run_action, step_outputs=step_outputs)
    return stop, step_outputs, calls, store


def test_workflow_parses_cleanly():
    parsed = parse_workflow(WF_PATH.read_text())
    assert parsed["id"] == "hub-task-closed-loop"
    assert parsed["enabled"] is True


@pytest.mark.parametrize(
    "payload,expected_status",
    [
        (
            {
                "event": "task_finished",
                "outcome": "finished",
                "intake_id": "f28baca0-22ba-4cd6-9103-d97bce48f31c",
                "session_id": "b863e11b-996c-52af-ac5c-8a3c66ef2dd3",
                "title": 'Fix "quotes"\nand newlines',
                "summary": "Merged PR #86 and deployed",
                "pr_url": "https://github.com/DataTalksClub/dapier/pull/86",
            },
            "Hub task finished:",
        ),
        (
            {
                "event": "task_blocked",
                "outcome": "unfinished",
                "intake_id": "abc-123",
                "summary": "HTTP 503 on /api/quota/grokbot",
            },
            "Hub task not finished (unfinished):",
        ),
        (
            {
                "event": "native_session_started",
                "intake_id": "abc-456",
                "session_id": "sess-1",
            },
            "Hub task started:",
        ),
    ],
)
def test_routes_to_sticky_hub_session_when_grokbot_below_threshold(payload, expected_status):
    def transport(method, url, headers=None, body=None):
        if url.endswith("/api/quota/grokbot"):
            return 200, json.dumps(USAGE_BODY_BELOW).encode("utf-8"), {"content-type": "application/json"}
        return 200, b'{"id":"msg-1"}', {"content-type": "application/json"}

    stop, step_outputs, calls, _ = _run_chain(FULL_STORAGE, payload, transport)
    assert stop is None
    assert step_outputs["route"]["output"]["branch"] == "then"
    hub_posts = [c for c in calls if c["url"] == "https://hub.dtcdev.click/api/messages"]
    assert len(hub_posts) == 1
    sent = json.loads(hub_posts[0]["body"].decode("utf-8"))
    assert sent["recipient"] == "agent:dev"
    assert sent["body"].startswith(expected_status)
    assert sent["key"].startswith("dapier-hub-task-")


def test_routes_to_dev_webhook_when_grokbot_usage_fine_or_503():
    for status, resp_body in (
        (200, json.dumps(USAGE_BODY_ABOVE).encode("utf-8")),
        (503, b'{"error":"schema_migration_deferred"}'),
    ):
        def transport(method, url, headers=None, body=None, _s=status, _b=resp_body):
            if url.endswith("/api/quota/grokbot"):
                return _s, _b, {"content-type": "application/json"}
            return 200, b'{"success":true}', {"content-type": "application/json"}

        stop, step_outputs, calls, _ = _run_chain(
            FULL_STORAGE,
            {"event": "task_finished", "outcome": "finished", "intake_id": "i-1", "title": "Test"},
            transport,
        )
        assert stop is None
        assert step_outputs["route"]["output"]["branch"] == "else"
        bot_posts = [c for c in calls if c["url"] == "https://bot.example.invalid/hook"]
        assert len(bot_posts) == 1
        sent = json.loads(bot_posts[0]["body"].decode("utf-8"))
        assert sent["status_text"] == "finished"
        assert "Hub task finished: Test" in sent["message"]


def test_rotates_hub_token_on_401():
    def transport(method, url, headers=None, body=None):
        if url.endswith("/api/quota/grokbot"):
            return 401, b'{"error":"unauthorized"}', {"content-type": "application/json"}
        if url.endswith("/api/token/refresh"):
            return 200, b'{"access_token":"new-tok","refresh_token":"new-ref"}', {"content-type": "application/json"}
        return 200, b'{"success":true}', {"content-type": "application/json"}

    stop, step_outputs, calls, store = _run_chain(
        FULL_STORAGE,
        {"event": "task_finished", "outcome": "finished", "intake_id": "i-2"},
        transport,
    )
    assert stop is None
    assert store["hub_api_token"] == "new-tok"
    assert store["hub_refresh_token"] == "new-ref"
    assert step_outputs["route"]["output"]["branch"] == "else"


def test_missing_webhook_config_fails_loudly():
    def transport(method, url, headers=None, body=None):
        return 200, b"{}", {"content-type": "application/json"}

    with pytest.raises(Exception, match="hub-task-closed-loop is not configured"):
        _run_chain({}, {"event": "task_finished"}, transport)
