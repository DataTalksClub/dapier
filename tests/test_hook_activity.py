"""The webhook delivery log behind the console Hooks tab: request records on
the envelope, the inbox's hook filter, the workflows a hook starts,
delivery outcomes, Send test request — over the admin route, the agent
route, and the CLI that wraps it (UI/CLI parity)."""
import json
import time

import pytest

from src.dapier.api import hook_activity
from src.dapier.api import router as ingress
from src.dapier.triggers import hook_triggers, inbox

SECRET = "whsec-test-1"


def _row(hook_id="orders", kind="webhook", token="tok-123", secret=None, enabled=True):
    item = {"hook_id": hook_id, "kind": kind, "url": f"https://d.test/hooks/{kind}/{hook_id}",
            "token": token, "enabled": enabled, "dedupe_path": ""}
    if secret:
        item["secret"] = secret
    return item


class HookTable:
    def __init__(self, items):
        self.items = {item["hook_id"]: dict(item) for item in items}

    def scan(self, **_kwargs):
        return {"Items": [dict(value) for value in self.items.values()]}

    def get_item(self, Key):
        item = self.items.get(Key["hook_id"])
        return {"Item": dict(item)} if item else {}


class InboxTable:
    def __init__(self):
        self.items = {}

    def put_item(self, Item, ConditionExpression=None):
        self.items.setdefault(Item["inbox_id"], dict(Item))

    def get_item(self, Key):
        item = self.items.get(Key["inbox_id"])
        return {"Item": dict(item)} if item else {}

    def scan(self, **_kwargs):
        return {"Items": [dict(value) for value in self.items.values()]}

    def update_item(self, Key, UpdateExpression, ExpressionAttributeNames, ExpressionAttributeValues):
        item = self.items[Key["inbox_id"]]
        item["status"] = ExpressionAttributeValues[":status"]
        if ":matched" in ExpressionAttributeValues:
            item["matched"] = ExpressionAttributeValues[":matched"]
        if ":error" in ExpressionAttributeValues:
            item["error"] = ExpressionAttributeValues[":error"]


@pytest.fixture
def env(monkeypatch):
    """Hook rows, a real _publish over a spied queue, and an inbox table
    the queue 'worker' records into."""
    state = {"hooks": HookTable([_row()]), "inbox": InboxTable(), "sent": []}

    def send_message(QueueUrl, MessageBody):
        envelope = json.loads(MessageBody)
        state["sent"].append(envelope)
        inbox.record(envelope, table_ref=state["inbox"])

    monkeypatch.setenv("EVENT_QUEUE_URL", "https://sqs.example.test/events")
    monkeypatch.setenv("HOOK_TRIGGERS_TABLE", "hooks")
    monkeypatch.setenv(inbox.TABLE_ENV, "inbox")
    monkeypatch.setattr(ingress.queue, "send_message", send_message)
    monkeypatch.setattr(hook_triggers, "get_table", lambda *a, **k: state["hooks"])
    monkeypatch.setattr(inbox, "_table", lambda: state["inbox"])
    monkeypatch.setattr(hook_activity, "_run_outcome", lambda wf, eid: state.get("runs", {}).get(f"{wf}:{eid}"))
    return state


def _post(headers, body=b'{"order": 7}', path="/hooks/webhook/orders"):
    response = ingress.handler({
        "requestContext": {"http": {"method": "POST", "path": path}},
        "headers": headers, "queryStringParameters": None, "body": body.decode(),
    }, None)
    return response["statusCode"], json.loads(response["body"])


# --- request records ------------------------------------------------------

def test_delivery_record_redacts_credentials_and_keeps_the_signature():
    record = hook_triggers.delivery_record(
        {"Authorization": "Bearer tok", "Cookie": "a=b", "X-Dapier-Signature": "sha256=ab",
         "User-Agent": "curl/8", "x-dapier-test": "1"}, b"12345", response_status=202)
    assert record["headers"]["authorization"] == hook_triggers.REDACTED
    assert record["headers"]["cookie"] == hook_triggers.REDACTED
    assert record["headers"]["x-dapier-signature"] == "sha256=ab"
    assert record["headers"]["user-agent"] == "curl/8"
    assert record["size_bytes"] == 5 and record["test"] is True
    assert record["response_status"] == 202


def test_a_delivery_carries_its_request_record_beside_unchanged_data(env):
    status, answer = _post({"authorization": "Bearer tok-123", "content-type": "application/json"})
    assert status == 202 and answer["accepted"] is True
    envelope = env["sent"][0]
    assert answer["event_id"] == envelope["id"]
    # workflows see exactly the payload they always did
    assert envelope["data"] == {"hook": "orders", "body": {"order": 7}, "query": {},
                                "content_type": "application/json"}
    assert envelope["request"]["headers"]["authorization"] == hook_triggers.REDACTED
    assert envelope["request"]["size_bytes"] == len(b'{"order": 7}')
    # and the inbox keeps it for the delivery log
    stored = inbox.api_get(envelope["id"], table_ref=env["inbox"])[1]["event"]
    assert stored["request"]["response_status"] == 202


def test_inbox_list_filters_by_hook_across_connectors(env):
    for hook_id, connector in (("orders", "webhook"), ("refunds", "webhook"), ("bot", "telegram"),
                               ("audience", "mailchimp"), ("x", "email")):
        inbox.record({"id": f"e-{hook_id}", "connector": connector, "event": "e",
                      "source": "list-1", "data": {"hook": hook_id}}, table_ref=env["inbox"])
    _, payload = inbox.api_list("webhook", hook="refunds", table_ref=env["inbox"])
    assert [event["inbox_id"] for event in payload["events"]] == ["e-refunds"]
    assert payload["paging"]["filtered"] is True
    _, payload = inbox.api_list(("webhook", "telegram", "mailchimp"), table_ref=env["inbox"])
    assert len(payload["events"]) == 4
    _, payload = inbox.api_list(None, hook="audience", table_ref=env["inbox"])
    assert [event["inbox_id"] for event in payload["events"]] == ["e-audience"]


def test_telegram_deliveries_join_the_log(env):
    inbox.record({"id": "tg-1", "connector": "telegram", "event": "message.received",
                  "source": "bot", "data": {"hook": "bot", "text": "hi"}}, table_ref=env["inbox"])
    row = hook_activity.api_deliveries("bot")[1]["deliveries"][0]
    assert row["kind"] == "telegram" and row["size_bytes"] > 0
    assert hook_activity.api_delivery("tg-1")[1]["delivery"]["body"]["text"] == "hi"


# --- which workflows a hook starts ------------------------------------------

def _flow(flow_id, filters=None, enabled=True):
    return {"id": flow_id, "enabled": enabled,
            "trigger": {"connector": "webhook", "event": "request.received",
                        "filters": filters or {}}}


def test_bound_workflows_reads_the_hook_filter():
    flows = [
        _flow("order-sync", {"hook": {"equals": "orders"}}),
        _flow("refund-sync", {"hook": {"equals": "refunds"}}),
        _flow("catch-all"),
        _flow("big-orders", {"hook": "orders", "body.total": {"gt": 100}}, enabled=False),
        {"id": "mail", "trigger": {"connector": "email", "event": "message.received"}},
    ]
    found = {flow["id"]: flow for flow in hook_activity.bound_workflows("orders", flows)}
    assert set(found) == {"order-sync", "catch-all", "big-orders"}
    assert found["catch-all"]["any_hook"] is True
    assert found["big-orders"]["conditional"] is True and found["big-orders"]["enabled"] is False
    assert found["order-sync"]["conditional"] is False


def test_attach_workflows_matches_each_kind_to_its_connector():
    hooks = [{"hook_id": "orders", "kind": "webhook"}, {"hook_id": "bot", "kind": "telegram"},
             {"hook_id": "yt", "kind": "youtube"}]
    telegram_flow = {"id": "tg-flow", "trigger": {"connector": "telegram", "event": "message.received",
                                                  "filters": {"hook": {"equals": "bot"}}}}
    hook_activity.attach_workflows(hooks, workflows=[
        _flow("order-sync", {"hook": {"equals": "orders"}}), telegram_flow])
    assert [flow["id"] for flow in hooks[0]["workflows"]] == ["order-sync"]
    assert [flow["id"] for flow in hooks[1]["workflows"]] == ["tg-flow"]
    assert "workflows" not in hooks[2]


# --- delivery outcomes --------------------------------------------------------

@pytest.mark.parametrize("event, runs, code", [
    ({"status": "received"}, [], "processing"),
    ({"status": "failed", "error": "boom"}, [], "failed"),
    ({"status": "unmatched"}, [], "no_workflow"),
    ({"status": "matched", "matched": ["a"]}, [{"workflow_id": "a", "status": "completed"}], "ran"),
    ({"status": "matched", "matched": ["a"]}, [{"workflow_id": "a", "status": "failed"}], "run_failed"),
    ({"status": "matched", "matched": ["a"]}, [{"workflow_id": "a", "status": "filtered"}], "filtered"),
    ({"status": "matched", "matched": ["a"]}, [{"workflow_id": "a", "status": "processing"}], "running"),
    ({"status": "matched", "matched": ["a"]}, [], "ran"),
])
def test_outcome_codes(event, runs, code):
    assert hook_activity.outcome(event, runs)[0] == code


def test_deliveries_list_and_detail(env):
    _post({"authorization": "Bearer tok-123", "content-type": "application/json",
           "x-dapier-test": "1"})
    event_id = env["sent"][0]["id"]
    inbox.complete(event_id, ["order-sync"], table_ref=env["inbox"])
    env["runs"] = {f"order-sync:{event_id}": {"run_id": f"order-sync:{event_id}",
                                              "workflow_id": "order-sync", "status": "completed"}}
    status, payload = hook_activity.api_deliveries("orders")
    assert status == 200
    row = payload["deliveries"][0]
    assert row["delivery_id"] == event_id and row["hook"] == "orders"
    assert row["outcome"] == "ran" and row["outcome_text"] == "Ran order-sync"
    assert row["test"] is True and row["response_status"] == 202
    assert row["runs"][0]["status"] == "completed"
    assert "headers" not in row  # the list stays light
    status, detail = hook_activity.api_delivery(event_id)
    assert status == 200
    assert detail["delivery"]["body"] == {"order": 7}
    assert detail["delivery"]["headers"]["authorization"] == hook_triggers.REDACTED
    assert hook_activity.api_deliveries("refunds")[1]["deliveries"] == []


def test_delivery_detail_refuses_other_connectors(env):
    inbox.record({"id": "mail-1", "connector": "email", "event": "message.received",
                  "data": {}}, table_ref=env["inbox"])
    assert hook_activity.api_delivery("mail-1")[0] == 404


# --- send test request ---------------------------------------------------------

def test_send_test_uses_the_bearer_token_and_lands_in_the_log(env):
    status, payload = hook_activity.api_send_test("orders", {"hello": "world"})
    assert status == 200
    assert payload["response"]["status"] == 202
    envelope = env["sent"][0]
    assert payload["delivery_id"] == envelope["id"]
    assert envelope["data"]["body"] == {"hello": "world"}
    assert envelope["request"]["test"] is True
    assert payload["request"]["headers"]["authorization"] == hook_triggers.REDACTED
    assert hook_activity.api_deliveries("orders")[1]["deliveries"][0]["test"] is True


def test_send_test_signs_a_secret_locked_hook(env):
    env["hooks"] = HookTable([_row(secret=SECRET)])
    status, payload = hook_activity.api_send_test("orders")
    assert status == 200 and payload["response"]["status"] == 202
    assert env["sent"][0]["data"]["body"] == hook_activity.TEST_SAMPLE
    assert payload["request"]["headers"]["x-dapier-signature"].startswith("sha256=")


def test_send_test_refusals(env):
    env["hooks"] = HookTable([_row(), _row("bot", kind="telegram"), _row("off", enabled=False)])
    assert hook_activity.api_send_test("missing")[0] == 404
    assert hook_activity.api_send_test("bot")[0] == 400
    assert hook_activity.api_send_test("off")[0] == 409
    assert hook_activity.api_send_test("orders", "text")[0] == 400
    assert env["sent"] == []


# --- routes: console (admin) and CLI (agent) -----------------------------------

def _admin_event(method, path, body=None, query=None, monkeypatch=None):
    from src.dapier.auth import session

    monkeypatch.setattr(session, "_credentials", lambda: {"username": "admin", "password": "pw"})
    cookie = session._sign({"sub": "op@datatalks.club", "subject": "op-sub",
                            "exp": int(time.time()) + 600})
    event = {"requestContext": {"http": {"method": method, "path": path}},
             "headers": {"host": "dapier.example.test", "origin": "https://dapier.example.test"},
             "cookies": [f"dapier_session={cookie}"], "queryStringParameters": query}
    if body is not None:
        event["body"] = json.dumps(body)
    return event


def test_admin_routes(env, monkeypatch):
    from src.dapier.api import admin as admin_api

    path = "/api/admin/hook-triggers/test"
    response = admin_api.route(_admin_event("POST", path, {"name": "orders"}, monkeypatch=monkeypatch),
                               "POST", path)
    assert response["statusCode"] == 200
    delivery_id = json.loads(response["body"])["delivery_id"]
    path = "/api/admin/hook-triggers/deliveries"
    response = admin_api.route(_admin_event("GET", path, query={"hook": "orders"},
                                            monkeypatch=monkeypatch), "GET", path)
    assert json.loads(response["body"])["deliveries"][0]["delivery_id"] == delivery_id
    path = f"/api/admin/hook-triggers/deliveries/{delivery_id}"
    response = admin_api.route(_admin_event("GET", path, monkeypatch=monkeypatch), "GET", path)
    assert json.loads(response["body"])["delivery"]["body"] == hook_activity.TEST_SAMPLE


def test_agent_routes(env, monkeypatch):
    from src.dapier.api import agent as agent_api

    monkeypatch.setattr("src.dapier.api.agent.authenticate", lambda event: ("sub-1", None))
    monkeypatch.setattr("src.dapier.api.agent.authz.is_operator", lambda payload: True)
    monkeypatch.setattr("src.dapier.api.agent.triggers._visibility", lambda event, subject: None)
    response = agent_api.route({"headers": {}, "body": json.dumps({"name": "orders"})},
                               "POST", "/api/agent/hook-triggers/test")
    assert response["statusCode"] == 200
    delivery_id = json.loads(response["body"])["delivery_id"]
    response = agent_api.route({"headers": {}, "queryStringParameters": {"hook": "orders"}},
                               "GET", "/api/agent/hook-triggers/deliveries")
    assert json.loads(response["body"])["deliveries"][0]["delivery_id"] == delivery_id
    response = agent_api.route({"headers": {}}, "GET",
                               f"/api/agent/hook-triggers/deliveries/{delivery_id}")
    assert response["statusCode"] == 200


# --- CLI ------------------------------------------------------------------------

def test_cli_hooks_activity_commands(monkeypatch, capsys, tmp_path):
    from dapier_cli import commands, main

    calls = []
    delivery = {"delivery_id": "e-1", "hook": "orders", "received_at": "2026-10-08T10:00:00+00:00",
                "outcome": "ran", "outcome_text": "Ran order-sync", "size_bytes": 12,
                "test": True, "runs": [{"run_id": "order-sync:e-1", "status": "completed"}],
                "headers": {"user-agent": "curl/8"}, "body": {"order": 7}}

    def fake_call(api_url, method, path, body=None, **kwargs):
        calls.append((method, path, body))
        if path.startswith("/api/agent/hook-triggers/deliveries/"):
            return {"delivery": delivery}
        if path.startswith("/api/agent/hook-triggers/deliveries"):
            return {"deliveries": [delivery], "paging": {}}
        return {"url": "https://d.test/hooks/webhook/orders", "delivery_id": "e-1",
                "response": {"status": 202, "body": {"accepted": True}}}

    monkeypatch.setattr(commands.api, "call", fake_call)
    monkeypatch.setattr("dapier_cli.commands.hooks.api.call", fake_call)
    payload = tmp_path / "data.json"
    payload.write_text('{"order": 7}')
    assert main.main(["hooks", "deliveries", "orders", "--limit", "5"]) == 0
    assert main.main(["webhooks", "delivery", "e-1"]) == 0
    assert main.main(["webhooks", "test", "orders", "--data", str(payload)]) == 0
    assert calls[0][:2] == ("GET", "/api/agent/hook-triggers/deliveries?limit=5&hook=orders")
    assert calls[1][:2] == ("GET", "/api/agent/hook-triggers/deliveries/e-1")
    assert calls[2] == ("POST", "/api/agent/hook-triggers/test", {"name": "orders", "data": {"order": 7}})
    out = capsys.readouterr().out
    assert "[test] Ran order-sync" in out
    assert "user-agent: curl/8" in out and '"order": 7' in out
    assert "response: 202" in out and "dapier hooks delivery e-1" in out


def test_cli_hooks_show_names_verification_and_workflows(monkeypatch, capsys):
    from dapier_cli import commands

    hook = {"hook_id": "orders", "kind": "webhook", "url": "u", "signed": True,
            "signature_header": "x-hub-signature-256",
            "workflows": [{"id": "order-sync", "enabled": True, "conditional": True}]}
    monkeypatch.setattr(commands.api, "call", lambda *a, **k: {"hooks": [hook]})
    assert commands.hooks_show("https://api.example.test", "orders") == 0
    out = capsys.readouterr().out
    assert "verification: signed — x-hub-signature-256" in out
    assert "starts: order-sync (when its filters pass)" in out
