"""Sync-response semantics (gap-analysis finding 6): the guarantees the
opt-in per-trigger ``response: {mode: sync}`` config must hold — exactly-once
inline-or-enqueue, single-match-only, the between-steps budget with its
enqueue fallback, the default and templated bodies, and the dedupe claim
holding across a sync run."""
import json

import boto3
import pytest

import src.dapier.engine.worker  # noqa: F401  # binds all_workflows pre-patch
from conftest import stubbed_action
from src.dapier.api import router as ingress
from src.dapier.triggers import hook_triggers


def _hook_stub(hook_id="orders", kind="webhook", token="tok-123", response=None,
               dedupe_path=None, actions=None):
    item = {"hook_id": hook_id, "kind": kind, "url": f"u-{hook_id}", "token": token,
            "actions": actions if actions is not None else [], "enabled": True,
            "dedupe_path": dedupe_path or ""}
    if response is not None:
        item["response"] = response
    return type("T", (), {
        "scan": lambda self, Limit=200: {"Items": [dict(item)]},
        "get_item": lambda self, Key: {"Item": dict(item)} if Key["hook_id"] == hook_id else {},
        "put_item": lambda self, Item: None,
        "delete_item": lambda self, Key: None,
    })()


def _post(body=b'{"hello":"world"}', headers=None, query=None, raw=False):
    response = ingress.handler({
        "requestContext": {"http": {"method": "POST", "path": "/hooks/webhook/orders"}},
        "headers": headers or {"authorization": "Bearer tok-123",
                               "content-type": "application/json"},
        "queryStringParameters": query,
        "body": body.decode(),
    }, None)
    if raw:
        return response["statusCode"], response["body"]
    parsed = response["body"]
    if response["headers"]["content-type"].startswith("application/json"):
        parsed = json.loads(parsed)
    return response["statusCode"], parsed


def _workflow(workflow_id="webhook-trigger-orders", actions=None):
    return {"id": workflow_id, "enabled": True,
            "trigger": {"connector": "webhook", "event": "request.received",
                        "filters": {"hook": {"equals": "orders"}}},
            "actions": actions if actions is not None else []}


def _own_workflow_run(event, before_action=None, after_action=None,
                      on_action_error=None, output=None):
    """The one matched workflow's chain, reduced to its step telemetry."""
    if after_action:
        after_action("webhook-trigger-orders", "notify", event,
                     output=output if output is not None else {"id": "m-1"},
                     duration_ms=5, status="completed")
    return ["webhook-trigger-orders"]


@pytest.fixture
def env(monkeypatch):
    """Router environment for the sync semantics: a swappable hook row, a
    spied queue (both the _publish seam and the raw SQS client), a matched-
    workflow list the single-match gate reads, and stubbed worker seams.
    ``state["workflows"]`` sets what matching returns (None = the hook's own
    workflow); ``state["sync"]`` replaces ``worker.execute``."""
    state = {"stub": {}, "published": [], "sent": [], "forgotten": [],
             "execute_calls": [], "gate_calls": [], "claims": [],
             "workflows": None,
             "sync": lambda event, **kwargs: _own_workflow_run(event, **kwargs)}

    def publish(connector, event_type, data, source=None, event_id=None, request=None):
        state["published"].append({"connector": connector, "data": data,
                                   "source": source, "id": event_id})

    def send_message(**kwargs):
        state["sent"].append(kwargs)

    def all_workflows():
        state["gate_calls"].append(1)
        if state["workflows"] is not None:
            return state["workflows"]
        return [_workflow()]

    def execute(event, **kwargs):
        state["execute_calls"].append(event)
        return state["sync"](event, **kwargs)

    def claim(scope, key, **kwargs):
        state["claims"].append((scope, key))
        return True

    def forget(scope, key, **kwargs):
        state["forgotten"].append((scope, key))

    def hook_table(*args, **kwargs):
        return _hook_stub(**state["stub"])

    def stub_attempt_hooks(attempt):
        # Router-level tests stub the production hooks (they write to the
        # executions table); the wrappers around them stay real. The writes
        # themselves are covered by the real-engine tests below.
        return {"before_action": lambda *a, **k: True,
                "after_action": lambda *a, **k: None,
                "on_action_error": lambda *a, **k: None}

    monkeypatch.setenv("EVENT_QUEUE_URL", "https://sqs.example.test/events")
    monkeypatch.setenv("HOOK_TRIGGERS_TABLE", "hooks")
    monkeypatch.setattr(ingress, "_publish", publish)
    monkeypatch.setattr(ingress.queue, "send_message", send_message)
    monkeypatch.setattr("src.dapier.engine.matching.all_workflows", all_workflows)
    monkeypatch.setattr("src.dapier.engine.worker.execute", execute)
    monkeypatch.setattr("src.dapier.engine.worker._attempt_hooks", stub_attempt_hooks)
    monkeypatch.setattr("src.dapier.triggers.seen.claim", claim)
    monkeypatch.setattr("src.dapier.triggers.seen.forget", forget)
    monkeypatch.setattr("src.dapier.triggers.hook_triggers.get_table", hook_table)
    return state


# --- config absent: byte-identical ack --------------------------------------

def test_config_absent_keeps_the_202_ack_byte_identical(env):
    # No "response" key on the stored item at all (every trigger saved
    # before the key existed): the ack path, untouched — same body bytes,
    # one publish, and the sync machinery (matching gate, inline run)
    # never even starts.
    env["stub"] = {}
    status, body = _post(raw=True)
    assert (status, body) == (202, '{"accepted": true}')
    assert len(env["published"]) == 1
    assert env["sent"] == [] and env["execute_calls"] == []
    assert env["gate_calls"] == []


def test_ack_mode_is_equally_untouched(env):
    env["stub"] = {"response": {"mode": "ack"}}
    status, body = _post(raw=True)
    assert (status, body) == (202, '{"accepted": true}')
    assert env["execute_calls"] == [] and env["gate_calls"] == []


# --- sync success: bodies ---------------------------------------------------

def test_sync_success_without_template_returns_the_run_step_outputs(env):
    # The default body is the run's step outputs (plus the workflow and the
    # event id, so the caller can find the run in history) — echoing the
    # trigger payload back would tell the caller nothing it does not have.
    env["stub"] = {"response": {"mode": "sync"}}
    status, body = _post(json.dumps({"name": "widget"}).encode())
    assert status == 200, body
    assert body == {"ok": True, "workflow": "webhook-trigger-orders",
                    "steps": {"notify": {"status": "completed",
                                         "output": {"id": "m-1"}}},
                    "event_id": body["event_id"]}
    assert body["event_id"]
    # Exactly-once: the inline run must not also be enqueued.
    assert env["published"] == [] and env["sent"] == []


def test_sync_success_renders_the_template_with_the_configured_status(env):
    env["stub"] = {"response": {"mode": "sync", "status": 201,
                                "template": {"got": "{trigger.body.name}",
                                             "step": "{steps.notify.output.id}"}}}
    status, body = _post(json.dumps({"name": "widget"}).encode())
    assert (status, body) == (201, {"got": "widget", "step": "m-1"})
    assert env["published"] == []


# --- single match only: the enqueue fallback --------------------------------

def test_multiple_matches_fall_back_to_202_without_running_inline(env):
    env["stub"] = {"response": {"mode": "sync"}}
    env["workflows"] = [_workflow("webhook-trigger-orders"), _workflow("orders-shadow")]
    status, body = _post()
    assert (status, body) == (202, {"accepted": True, "mode": "async",
                                    "reason": "multiple_matches"})
    # Enqueue fallback does not run inline: one publish, zero inline runs.
    assert len(env["published"]) == 1 and env["execute_calls"] == []
    assert env["sent"] == []


def test_zero_matches_fall_back_to_202(env):
    env["stub"] = {"response": {"mode": "sync"}}
    env["workflows"] = []
    status, body = _post()
    assert (status, body) == (202, {"accepted": True, "mode": "async",
                                    "reason": "no_match"})
    assert len(env["published"]) == 1 and env["execute_calls"] == []


def test_fallback_publish_failure_releases_the_claim(env, monkeypatch):
    """The fallback IS the queue path: a failed publish releases the claim
    and surfaces the error, so the provider's retry delivers — exactly like
    the ack path's publish failure."""
    env["stub"] = {"response": {"mode": "sync"}}
    env["workflows"] = []

    def broken_publish(*args, **kwargs):
        raise RuntimeError("queue down")

    monkeypatch.setattr(ingress, "_publish", broken_publish)
    with pytest.raises(RuntimeError):
        _post()
    assert env["forgotten"] and env["execute_calls"] == []


# --- exactly-once: the fallback reuses the inline run's event id ------------

def test_budget_overrun_falls_back_and_enqueues_under_the_same_event_id(env, monkeypatch):
    """The budget is checked between steps (never mid-step): the run stops at
    the next step boundary without side effects, the event is enqueued under
    the id the inline steps were leased under, and the worker finishes the
    un-run tail — the replay skips the completed steps via those leases."""
    env["stub"] = {"response": {"mode": "sync", "budget_seconds": 1}}
    clock = {"now": 0.0}

    class _FakeTime:
        def monotonic(self):
            return clock["now"]

    monkeypatch.setattr(ingress, "time", _FakeTime())

    def run(event, before_action=None, after_action=None, on_action_error=None):
        # Step 1 starts inside the budget and completes.
        assert before_action("webhook-trigger-orders", "s1", event, "webhook") is True
        after_action("webhook-trigger-orders", "s1", event,
                     output={"n": 1}, duration_ms=1, status="completed")
        # ... the slow step pushes past the deadline; step 2 is skipped
        # in place (False = no lease, no side effect).
        clock["now"] = 5.0
        assert before_action("webhook-trigger-orders", "s2", event, "webhook") is False
        return ["webhook-trigger-orders"]

    env["sync"] = run
    status, body = _post()
    assert (status, body) == (202, {"accepted": True, "mode": "async",
                                    "reason": "budget"})
    assert len(env["published"]) == 1 and env["sent"] == []
    # The enqueue carries the exact id the completed step was leased under,
    # so the worker's replay dedupes it.
    assert env["published"][0]["id"] == env["execute_calls"][0]["id"]


# --- exactly-once: the dedupe claim still holds across a sync run ------------

def test_dedupe_claim_holds_a_sync_delivery_replay_does_not_double_run(env, monkeypatch):
    env["stub"] = {"response": {"mode": "sync"}, "dedupe_path": "event.id"}
    fresh = {"value": True}

    def claim(scope, key, **kwargs):
        state = fresh["value"]
        fresh["value"] = False  # the same delivery is only fresh once
        return state

    monkeypatch.setattr("src.dapier.triggers.seen.claim", claim)
    body = json.dumps({"event": {"id": "d-9"}}).encode()

    status, first = _post(body)
    assert status == 200 and first["ok"] is True
    assert len(env["execute_calls"]) == 1

    status, second = _post(body)
    assert status == 200 and second["duplicate"] is True
    assert second["event_id"] == first["event_id"]
    # Still exactly one inline run, and still nothing enqueued.
    assert len(env["execute_calls"]) == 1
    assert env["published"] == [] and env["sent"] == []


# --- the config keys ---------------------------------------------------------

def test_sync_status_and_budget_config_validation():
    assert hook_triggers.validate_response(
        {"mode": "sync", "status": 201, "budget_seconds": 25}, "webhook") == {
            "mode": "sync", "status": 201, "budget_seconds": 25}
    # Keys stay absent when unset (configs saved before they existed).
    assert hook_triggers.validate_response({"mode": "sync"}, "webhook") == {"mode": "sync"}
    assert hook_triggers.validate_response(
        {"mode": "sync", "budget_seconds": 999}, "webhook") == {
            "mode": "sync", "budget_seconds": 25}  # clamped at the cap
    with pytest.raises(hook_triggers.TriggerError):
        hook_triggers.validate_response({"mode": "sync", "status": 999}, "webhook")
    with pytest.raises(hook_triggers.TriggerError):
        hook_triggers.validate_response({"mode": "sync", "status": "ok"}, "webhook")
    with pytest.raises(hook_triggers.TriggerError):
        hook_triggers.validate_response({"mode": "sync", "budget_seconds": "soon"}, "webhook")
    with pytest.raises(hook_triggers.TriggerError):
        hook_triggers.validate_response({"mode": "sync", "bogus": 1}, "webhook")


def test_status_and_budget_read_back_with_defaults_for_old_items():
    # Stored items saved before the keys existed (or hand-edited) read as
    # the defaults at delivery time — never an error, never a 5xx.
    assert hook_triggers.response_status({"response": {"mode": "sync"}}) == 200
    assert hook_triggers.response_budget({"response": {"mode": "sync"}}) == 10
    assert hook_triggers.response_status({}) == 200
    assert hook_triggers.response_budget({}) == 10
    assert hook_triggers.response_budget({"response": {"budget_seconds": 999}}) == 25
    assert hook_triggers.response_budget({"response": {"budget_seconds": 0}}) == 1
    assert hook_triggers.response_status({"response": {"status": 999}}) == 200
    assert hook_triggers.response_status({"response": {"status": "201"}}) == 201


# --- end to end: the real engine runs the stored trigger's own workflow -----

class _Table:
    """Captures the step-lease writes the inline run makes, so a test can
    assert the exactly-once linkage between the inline run and a replay."""

    def __init__(self):
        self.items = []

    def put_item(self, **kwargs):
        self.items.append(("processing", kwargs["Item"]["execution_id"],
                           kwargs["Item"].get("action_id")))

    def update_item(self, **kwargs):
        self.items.append((kwargs["ExpressionAttributeValues"][":status"],
                           kwargs["Key"]["execution_id"], None))

    def get_item(self, **kwargs):
        return {}

    def query(self, **kwargs):
        return {"Items": []}


@pytest.fixture
def real_engine(monkeypatch, tmp_path):
    """No worker seams stubbed: the real engine executes the designer flow
    bound to the hook (matching loads it from the managed-store seam), with
    the executions table captured and every other workflow source (bundled
    YAML, published, email/schedule/poll triggers) isolated away."""
    monkeypatch.setenv("EVENT_QUEUE_URL", "https://sqs.example.test/events")
    monkeypatch.setenv("HOOK_TRIGGERS_TABLE", "hooks")
    monkeypatch.setenv("EXECUTIONS_TABLE", "executions")
    monkeypatch.setenv("WORKFLOWS_DIR", str(tmp_path))  # empty: only the hook's workflow
    for unused in ("TASK_USAGE_TABLE", "PUBLISHED_WORKFLOWS_TABLE",
                   "SCHEDULE_TRIGGERS_TABLE",
                   "POLL_TRIGGERS_TABLE", "TRIGGER_INBOX_TABLE"):
        monkeypatch.delenv(unused, raising=False)
    state = {"published": [], "sent": [], "table": _Table(), "runs": []}

    def publish(connector, event_type, data, source=None, event_id=None, request=None):
        state["published"].append({"connector": connector, "data": data, "id": event_id})

    def send_message(**kwargs):
        state["sent"].append(kwargs)

    class Dynamo:
        def Table(self, _name):
            return state["table"]

    monkeypatch.setattr(boto3, "resource", lambda service: Dynamo())
    monkeypatch.setattr(ingress, "_publish", publish)
    monkeypatch.setattr(ingress.queue, "send_message", send_message)
    monkeypatch.setattr("src.dapier.triggers.seen.claim", lambda scope, key, **k: True)
    monkeypatch.setattr("src.dapier.triggers.hook_triggers.get_table",
                        lambda *a, **k: _hook_stub(response={"mode": "sync"}))
    monkeypatch.setattr("src.dapier.engine.matching.workflows",
                        lambda: [_workflow(actions=[
                            {"id": "ack", "type": "webhook",
                             "url": "https://target.test/x"}])])
    return state


def test_real_inline_run_answers_step_outputs_without_enqueueing(real_engine):
    with stubbed_action("webhook", lambda action, event: real_engine["runs"].append(
            action["id"]) or {"delivered": True}):
        status, body = _post(json.dumps({"name": "widget"}).encode())
    assert status == 200
    assert body["ok"] is True
    assert body["workflow"] == "webhook-trigger-orders"
    assert body["steps"] == {"ack": {"status": "completed",
                                     "output": {"delivered": True}}}
    # The inline run does not enqueue: the SQS client saw nothing.
    assert real_engine["sent"] == [] and real_engine["published"] == []
    # The step ran exactly once, leased under the delivery's event id.
    assert real_engine["runs"] == ["ack"]
    processing = [entry for entry in real_engine["table"].items if entry[0] == "processing"]
    assert processing and processing[0][2] == "ack"
    assert processing[0][1] == f"webhook-trigger-orders:ack:{body['event_id']}"


def test_real_budget_overrun_falls_back_to_the_queue(real_engine, monkeypatch):
    clock = {"now": 0.0}

    class _FakeTime:
        def monotonic(self):
            return clock["now"]

    monkeypatch.setattr(ingress, "time", _FakeTime())
    monkeypatch.setattr("src.dapier.triggers.hook_triggers.get_table",
                        lambda *a, **k: _hook_stub(
                            response={"mode": "sync", "budget_seconds": 1}))
    monkeypatch.setattr("src.dapier.engine.matching.workflows",
                        lambda: [_workflow(actions=[
                            {"id": "slow", "type": "webhook",
                             "url": "https://target.test/x"},
                            {"id": "never", "type": "webhook",
                             "url": "https://target.test/y"}])])

    def runner(action, event):
        real_engine["runs"].append(action["id"])
        clock["now"] = 60.0  # the slow action burns the whole budget
        return {"slept": True}

    with stubbed_action("webhook", runner):
        status, body = _post()
    assert (status, body) == (202, {"accepted": True, "mode": "async",
                                    "reason": "budget"})
    # The un-run tail never executed inline; the enqueue owns it now.
    assert real_engine["runs"] == ["slow"]
    assert len(real_engine["published"]) == 1 and real_engine["sent"] == []
    # The completed step's lease and the enqueued event share the event id.
    published_id = real_engine["published"][0]["id"]
    assert any(f"webhook-trigger-orders:slow:{published_id}" == entry[1]
               for entry in real_engine["table"].items)
    assert not any("never" in (entry[2] or "") for entry in real_engine["table"].items)
