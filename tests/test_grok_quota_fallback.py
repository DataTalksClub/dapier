"""GrokBot <1% quota fallback: mailbox workflows divert to sticky Hub sessions.

Contract (Hub intake 408b585e, Dapier intake f28baca0):
- Each mailbox workflow (orin / tofu / dev) checks Hub's GrokBot usage export
  (GET /api/quota/grokbot) only when the four hub_* storage keys are armed.
- The route diverts to the role's sticky Hub session (POST /api/messages,
  recipient agent:<role>, body "Hey, check your emails.") only when the
  export says below_threshold on fresh evidence — the same rule Hub applies,
  so stale or unknown evidence never diverts.
- Everything else (missing config, skipped/failed check, stale evidence)
  keeps the historical webhook delivery: fail-safe, a Hub outage can never
  divert mailbox mail into the void.
- The messages key is dapier-<role>-<sha256(message id)> — stable across
  queue redeliveries of the same email, so Hub-side dedupe absorbs retries.
- dev-mailbox's direct oncall ingest always runs, independent of the route.
"""
import hashlib
import json
from pathlib import Path
import pytest
import yaml

from src.dapier.connectors.webhook import run_http_request
from src.dapier.engine.actions.code import run_code
from src.dapier.engine.logic_pkg.chains import run_chain
from src.dapier.engine.logic_pkg.core import evaluate_rules, predicate_rules

MAILBOXES = (
    ("orin-mailbox", "orin", "agent:orin"),
    ("tofu-mailbox", "tofu", "agent:tofu"),
    ("dev-mailbox", "dev", "agent:dev"),
)
USAGE_BODY_BELOW = {
    "account": "grokbot", "provider": "grok", "remaining_percent": 0.7,
    "threshold_percent": 1, "below_threshold": True, "fresh": True,
    "state": "fresh", "observed_at": "2026-11-19T00:00:00Z", "reset_at": None,
}
USAGE_BODY_ABOVE = {
    "account": "grokbot", "provider": "grok", "remaining_percent": 42.0,
    "threshold_percent": 1, "below_threshold": False, "fresh": True,
    "state": "fresh", "observed_at": "2026-11-19T00:00:00Z", "reset_at": None,
}
USAGE_BODY_STALE = {
    "account": "grokbot", "provider": "grok", "remaining_percent": None,
    "threshold_percent": 1, "below_threshold": False, "fresh": False,
    "state": "stale", "observed_at": "2026-11-18T00:00:00Z", "reset_at": None,
}
HUB_KEYS_CONFIGURED = {
    "get-hub-usage-url": {"status": "completed", "output": {"value": "https://hub.dtcdev.click/api/quota/grokbot"}},
    "get-hub-messages-url": {"status": "completed", "output": {"value": "https://hub.dtcdev.click/api/messages"}},
    "get-hub-api-token": {"status": "completed", "output": {"value": "hub-token"}},
    "get-hub-recipient": {"status": "completed", "output": {"value": "agent:orin"}},
}


def _load(workflow_id):
    wf_path = Path(__file__).parent.parent / f"workflows/{workflow_id}.yaml"
    return yaml.safe_load(wf_path.read_text())


def _steps(wf):
    def walk(steps):
        for step in steps or []:
            yield step
            for branch in ("then", "else"):
                yield from walk(step.get(branch))
    yield from walk(wf.get("actions"))


def _one(wf, step_id):
    return next(a for a in _steps(wf) if a.get("id") == step_id)


def _decide(workflow_id, *, check=None, hub_keys=True):
    wf = _load(workflow_id)
    decide = _one(wf, "decide-grok-route")
    steps = {k: dict(v) for k, v in HUB_KEYS_CONFIGURED.items()}
    if not hub_keys:
        for key in ("get-hub-usage-url", "get-hub-messages-url", "get-hub-api-token", "get-hub-recipient"):
            steps[key] = {"status": "completed", "output": {"key": key, "value": "", "found": False}}
    if check is not None:
        status, body = check
        output = {"body": body} if body is not None else {}
        steps["grok-fallback-armed.then.check-grokbot-usage"] = {"status": status, "output": output}
    return run_code(decide, {"connector": "email", "data": {}}, steps=steps)["result"]


# --- definition -------------------------------------------------------------


@pytest.mark.parametrize("workflow_id,role,recipient", MAILBOXES)
def test_fallback_definition(workflow_id, role, recipient):
    wf = _load(workflow_id)

    armed = next(a for a in wf["actions"] if a.get("id") == "grok-fallback-armed")
    assert armed["type"] == "condition"
    rules = armed.get("when") or {}
    assert rules == {
        "steps.get-hub-usage-url.output.value": {"prefix": "http"},
        "steps.get-hub-messages-url.output.value": {"prefix": "http"},
        "steps.get-hub-api-token.output.value": {"exists": True},
        "steps.get-hub-recipient.output.value": {"exists": True},
    }
    check = armed.get("then") or []
    assert [a.get("id") for a in check] == ["check-grokbot-usage"]
    assert check[0]["method"] == "GET"
    assert check[0]["url"] == "{steps.get-hub-usage-url.output.value}"
    assert check[0]["auth_token"] == "{steps.get-hub-api-token.output.value}"
    assert check[0]["on_fail"] == "continue"
    assert armed.get("else") == []

    route = next(a for a in wf["actions"] if a.get("id") == "route")
    assert route["type"] == "condition"
    assert route["when"] == {"steps.decide-grok-route.output.result.route": {"equals": "hub_session"}}
    notify = (route.get("then") or [])
    assert [a.get("id") for a in notify] == ["notify-hub-session"]
    assert notify[0]["method"] == "POST"
    assert notify[0]["url"] == "{steps.get-hub-messages-url.output.value}"
    assert notify[0]["auth_token"] == "{steps.get-hub-api-token.output.value}"
    assert notify[0]["content_type"] == "application/json"
    body = notify[0]["body"]
    assert '"body": "Hey, check your emails."' in body
    assert '"recipient": "{steps.get-hub-recipient.output.value}"' in body
    assert f'"key": "dapier-{role}-' + "{steps." in body
    webhook = (route.get("else") or [])
    assert [a.get("id") for a in webhook] == ["post-webhook"]
    assert webhook[0]["body"] == "{steps.format-email.output.result}"

    decide = _one(wf, "decide-grok-route")
    assert decide["type"] == "code"
    assert "below_threshold" in decide["code"]
    assert "fresh" in decide["code"]
    # No secrets in the YAML.
    assert "hub-token" not in yaml.safe_dump(wf)


@pytest.mark.parametrize("workflow_id,role,recipient", MAILBOXES)
def test_fallback_armed_only_when_fully_configured(workflow_id, role, recipient):
    wf = _load(workflow_id)
    armed = next(a for a in wf["actions"] if a.get("id") == "grok-fallback-armed")
    rules = predicate_rules(armed)
    data = {"steps": HUB_KEYS_CONFIGURED}
    assert evaluate_rules(rules, data) is True
    for missing in ("get-hub-usage-url", "get-hub-messages-url", "get-hub-api-token", "get-hub-recipient"):
        partial = {k: v for k, v in HUB_KEYS_CONFIGURED.items() if k != missing}
        assert evaluate_rules(rules, {"steps": partial}) is False
        empty = {**HUB_KEYS_CONFIGURED, missing: {"status": "completed", "output": {"value": "", "found": False}}}
        assert evaluate_rules(rules, {"steps": empty}) is False


# --- decide-grok-route: the divert decision ---------------------------------


@pytest.mark.parametrize("workflow_id,role,recipient", MAILBOXES)
def test_decide_diverts_on_fresh_below_threshold(workflow_id, role, recipient):
    result = _decide(workflow_id, check=("completed", USAGE_BODY_BELOW))
    assert result["route"] == "hub_session"
    assert result["reason"] == "grokbot_below_threshold"
    assert result["recipient"] == "agent:orin"


@pytest.mark.parametrize("workflow_id,role,recipient", MAILBOXES)
def test_decide_keeps_webhook_when_above_threshold(workflow_id, role, recipient):
    result = _decide(workflow_id, check=("completed", USAGE_BODY_ABOVE))
    assert result == {"route": "webhook", "reason": "grokbot_above_threshold", "recipient": ""}


@pytest.mark.parametrize("workflow_id,role,recipient", MAILBOXES)
def test_decide_keeps_webhook_on_stale_evidence(workflow_id, role, recipient):
    result = _decide(workflow_id, check=("completed", USAGE_BODY_STALE))
    assert result == {"route": "webhook", "reason": "grokbot_usage_not_fresh", "recipient": ""}
    # below_threshold with stale evidence is still refused (defense in depth:
    # Hub promises below_threshold only on fresh evidence; Dapier re-checks).
    stale_below = {**USAGE_BODY_STALE, "below_threshold": True}
    result = _decide(workflow_id, check=("completed", stale_below))
    assert result["route"] == "webhook"


@pytest.mark.parametrize("workflow_id,role,recipient", MAILBOXES)
def test_decide_keeps_webhook_when_usage_check_skipped(workflow_id, role, recipient):
    result = _decide(workflow_id, check=("skipped", None))
    assert result == {"route": "webhook", "reason": "grokbot_usage_unavailable", "recipient": ""}


@pytest.mark.parametrize("workflow_id,role,recipient", MAILBOXES)
def test_decide_keeps_webhook_without_check_step(workflow_id, role, recipient):
    result = _decide(workflow_id)
    assert result == {"route": "webhook", "reason": "grokbot_usage_unavailable", "recipient": ""}


@pytest.mark.parametrize("workflow_id,role,recipient", MAILBOXES)
def test_decide_keeps_webhook_when_fallback_not_configured(workflow_id, role, recipient):
    result = _decide(workflow_id, check=("completed", USAGE_BODY_BELOW), hub_keys=False)
    assert result == {"route": "webhook", "reason": "hub_fallback_not_configured", "recipient": ""}


@pytest.mark.parametrize("workflow_id,role,recipient", MAILBOXES)
def test_decide_keeps_webhook_without_recipient(workflow_id, role, recipient):
    wf = _load(workflow_id)
    decide = _one(wf, "decide-grok-route")
    steps = {**HUB_KEYS_CONFIGURED,
             "get-hub-recipient": {"status": "completed", "output": {"value": "", "found": False}},
             "grok-fallback-armed.then.check-grokbot-usage": {"status": "completed", "output": {"body": USAGE_BODY_BELOW}}}
    result = run_code(decide, {"connector": "email", "data": {}}, steps=steps)["result"]
    assert result == {"route": "webhook", "reason": "hub_fallback_not_configured", "recipient": ""}


# --- delivery: usage GET and sticky-session POST -----------------------------


@pytest.mark.parametrize("workflow_id,role,recipient", MAILBOXES)
def test_usage_check_requests_hub_export(workflow_id, role, recipient):
    wf = _load(workflow_id)
    check = _one(wf, "check-grokbot-usage")
    event = {"connector": "email", "data": {}}
    captured = {}

    def transport(method, url, headers=None, body=None, timeout=None):
        captured.update(method=method, url=url, headers=headers, body=body)
        return 200, json.dumps(USAGE_BODY_BELOW).encode("utf-8"), {"content-type": "application/json"}

    result = run_http_request(check, event, steps=HUB_KEYS_CONFIGURED, transport=transport)
    assert captured["method"] == "GET"
    assert captured["url"] == "https://hub.dtcdev.click/api/quota/grokbot"
    assert captured["headers"]["authorization"] == "Bearer hub-token"
    assert result["body"]["below_threshold"] is True
    assert result["status"] == 200


@pytest.mark.parametrize("workflow_id,role,recipient", MAILBOXES)
def test_notify_posts_to_role_sticky_session(workflow_id, role, recipient):
    wf = _load(workflow_id)
    notify = _one(wf, "notify-hub-session")
    event = {"connector": "email", "data": {}}
    steps = {**HUB_KEYS_CONFIGURED,
             "get-hub-recipient": {"status": "completed", "output": {"value": recipient}},
             "hub-delivery-id": {"status": "completed", "output": {"result": "b" * 64}}}
    if workflow_id == "dev-mailbox":
        steps["oncall-delivery-id"] = {"status": "completed", "output": {"result": "b" * 64}}
    captured = {}

    def transport(method, url, headers=None, body=None, timeout=None):
        captured.update(method=method, url=url, headers=headers, body=body)
        return 200, b'{"id": "msg-1"}', {"content-type": "application/json"}

    result = run_http_request(notify, event, steps=steps, transport=transport)
    assert result["status"] == 200
    assert captured["method"] == "POST"
    assert captured["url"] == "https://hub.dtcdev.click/api/messages"
    assert captured["headers"]["authorization"] == "Bearer hub-token"
    assert captured["headers"]["content-type"] == "application/json"
    sent = json.loads(captured["body"].decode("utf-8"))
    assert sent == {"recipient": recipient,
                    "body": "Hey, check your emails.",
                    "key": f"dapier-{role}-" + "b" * 64}


def test_delivery_id_stable_across_retries():
    wf = _load("orin-mailbox")
    id_action = _one(wf, "hub-delivery-id")
    payload = {"message_id": "<stable@example.invalid>", "subject": "Hi"}
    steps = {"format-email": {"status": "completed", "output": {"result": payload}}}
    first = run_code(id_action, {"connector": "email", "data": {}}, steps=steps)["result"]
    second = run_code(id_action, {"connector": "email", "data": {}}, steps=steps)["result"]
    assert first == second
    assert len(first) == 64
    # A different email gets a different key.
    other = dict(payload, message_id="<other@example.invalid>")
    steps["format-email"]["output"]["result"] = other
    assert run_code(id_action, {"connector": "email", "data": {}}, steps=steps)["result"] != first


# --- full-chain behavior through the real engine -----------------------------


def _run_chain(workflow_id, storage, usage_response):
    wf = _load(workflow_id)
    calls = []

    def transport(method, url, headers=None, body=None, timeout=None):
        calls.append({"method": method, "url": url, "headers": headers, "body": body})
        if url.endswith("/api/quota/grokbot"):
            return 200, json.dumps(usage_response).encode("utf-8"), {"content-type": "application/json"}
        return 200, b'{"accepted": true}', {"content-type": "application/json"}

    def run_action(step, event, workflow_id, steps=None):
        kind = step.get("type")
        if kind == "storage_get":
            key = step.get("key")
            found = key in storage
            return {"key": key, "value": storage.get(key, ""), "found": found}
        if kind == "code":
            return run_code(step, event, steps=steps)
        if kind == "http_request":
            return run_http_request(step, event, steps=steps, transport=transport)
        raise AssertionError(f"unexpected step type {kind}")

    event = {
        "connector": "email",
        "data": {
            "route": workflow_id.split("-")[0],
            "from": "alexey@datatalks.club",
            "to": f"{workflow_id.split('-')[0]}@dtcdev.click",
            "subject": "Quota fallback chain test",
            "date": "2026-11-19T00:00:00Z",
            "message_id": f"<chain-{workflow_id}@mail.test>",
            "body": {"text": "Hello there"},
            "attachments": [],
        },
    }
    step_outputs = {}
    stop = run_chain(workflow_id, wf["actions"], event, run_action, step_outputs=step_outputs)
    return stop, step_outputs, calls


FULL_STORAGE = {
    "webhook_url": "https://bot.example.invalid/hook",
    "webhook_token": "bot-token",
    "hub_usage_url": "https://hub.dtcdev.click/api/quota/grokbot",
    "hub_messages_url": "https://hub.dtcdev.click/api/messages",
    "hub_api_token": "hub-token",
    "hub_recipient": "agent:orin",
}


@pytest.mark.parametrize("workflow_id", ["orin-mailbox", "tofu-mailbox"])
def test_chain_diverts_to_sticky_session_when_below_threshold(workflow_id):
    storage = {**FULL_STORAGE, "hub_recipient": f"agent:{workflow_id.split('-')[0]}"}
    stop, step_outputs, calls = _run_chain(workflow_id, storage, USAGE_BODY_BELOW)
    assert stop is None
    assert step_outputs["route"]["output"]["branch"] == "then"
    assert step_outputs["decide-grok-route"]["output"]["result"]["route"] == "hub_session"
    urls = [c["url"] for c in calls]
    assert "https://bot.example.invalid/hook" not in urls
    notifies = [c for c in calls if c["url"] == "https://hub.dtcdev.click/api/messages"]
    assert len(notifies) == 1
    sent = json.loads(notifies[0]["body"].decode("utf-8"))
    role = workflow_id.split("-")[0]
    expected_key = "dapier-%s-%s" % (role, hashlib.sha256(f"<chain-{workflow_id}@mail.test>".encode()).hexdigest())
    assert sent["recipient"] == f"agent:{role}"
    assert sent["body"] == "Hey, check your emails."
    assert sent["key"] == expected_key
    notify_entry = step_outputs["route.then.notify-hub-session"]
    assert notify_entry["status"] == "completed"


@pytest.mark.parametrize("workflow_id", ["orin-mailbox", "tofu-mailbox"])
def test_chain_posts_webhook_when_usage_fine(workflow_id):
    stop, step_outputs, calls = _run_chain(workflow_id, FULL_STORAGE, USAGE_BODY_ABOVE)
    assert stop is None
    assert step_outputs["route"]["output"]["branch"] == "else"
    assert step_outputs["decide-grok-route"]["output"]["result"]["route"] == "webhook"
    assert all(c["url"] != "https://hub.dtcdev.click/api/messages" for c in calls)
    hooks = [c for c in calls if c["url"] == "https://bot.example.invalid/hook"]
    assert len(hooks) == 1
    sent = json.loads(hooks[0]["body"].decode("utf-8"))
    assert sent["subject"] == "Quota fallback chain test"
    assert sent["snippet"] == "Hello there"
    assert hooks[0]["headers"]["authorization"] == "Bearer bot-token"


@pytest.mark.parametrize("workflow_id", ["orin-mailbox", "tofu-mailbox"])
def test_chain_falls_back_to_webhook_when_hub_unreachable(workflow_id):
    """A Hub outage (usage check skipped) keeps the historical webhook route."""
    wf = _load(workflow_id)

    def transport(method, url, headers=None, body=None, timeout=None):
        if url.endswith("/api/quota/grokbot"):
            raise ConnectionError("hub down")
        return 200, b'{"accepted": true}', {"content-type": "application/json"}

    def run_action(step, event, workflow_id, steps=None):
        kind = step.get("type")
        if kind == "storage_get":
            key = step.get("key")
            return {"key": key, "value": FULL_STORAGE.get(key, ""), "found": key in FULL_STORAGE}
        if kind == "code":
            return run_code(step, event, steps=steps)
        if kind == "http_request":
            return run_http_request(step, event, steps=steps, transport=transport)
        raise AssertionError(kind)

    event = {"connector": "email", "data": {"message_id": "<hub-down@mail.test>",
                                            "subject": "s", "body": {"text": "b"}}}
    step_outputs = {}
    stop = run_chain(workflow_id, wf["actions"], event, run_action, step_outputs=step_outputs)
    assert stop is None
    assert step_outputs["route"]["output"]["branch"] == "else"
    assert step_outputs["decide-grok-route"]["output"]["result"]["reason"] == "grokbot_usage_unavailable"


@pytest.mark.parametrize("workflow_id", ["orin-mailbox", "tofu-mailbox"])
def test_chain_skips_hub_entirely_when_not_armed(workflow_id):
    storage = {k: v for k, v in FULL_STORAGE.items() if not k.startswith("hub_")}
    stop, step_outputs, calls = _run_chain(workflow_id, storage, USAGE_BODY_BELOW)
    assert stop is None
    assert step_outputs["grok-fallback-armed"]["output"]["branch"] == "else"
    assert all(c["url"] != "https://hub.dtcdev.click/api/quota/grokbot" for c in calls)
    assert step_outputs["route"]["output"]["branch"] == "else"
    assert [c["url"] for c in calls].count("https://bot.example.invalid/hook") == 1


def test_dev_chain_diverts_but_always_ingests_oncall():
    storage = {**FULL_STORAGE, "hub_recipient": "agent:dev",
               "oncall_webhook_url": "https://hub.dtcdev.click/webhooks/oncall",
               "oncall_webhook_token": "oncall-token"}
    stop, step_outputs, calls = _run_chain("dev-mailbox", storage, USAGE_BODY_BELOW)
    assert stop is None
    assert step_outputs["route"]["output"]["branch"] == "then"
    urls = [c["url"] for c in calls]
    assert "https://bot.example.invalid/hook" not in urls
    assert urls.count("https://hub.dtcdev.click/webhooks/oncall") == 1
    assert urls.count("https://hub.dtcdev.click/api/messages") == 1
    oncall = next(c for c in calls if c["url"] == "https://hub.dtcdev.click/webhooks/oncall")
    assert oncall["headers"]["X-Delivery-Id"] == step_outputs["oncall-delivery-id"]["output"]["result"]
    assert step_outputs["verify-delivery"]["status"] == "completed"
    assert step_outputs["verify-delivery"]["output"]["result"]["routed_to"] == "then"


def test_dev_chain_webhook_route_when_usage_fine():
    storage = {**FULL_STORAGE, "hub_recipient": "agent:dev",
               "oncall_webhook_url": "https://hub.dtcdev.click/webhooks/oncall",
               "oncall_webhook_token": "oncall-token"}
    stop, step_outputs, calls = _run_chain("dev-mailbox", storage, USAGE_BODY_ABOVE)
    assert stop is None
    assert step_outputs["route"]["output"]["branch"] == "else"
    urls = [c["url"] for c in calls]
    assert urls.count("https://bot.example.invalid/hook") == 1
    assert urls.count("https://hub.dtcdev.click/webhooks/oncall") == 1
    assert step_outputs["verify-delivery"]["status"] == "completed"
