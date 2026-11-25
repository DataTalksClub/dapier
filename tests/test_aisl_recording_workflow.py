"""Execute the shipped recording router with mocked I/O, never live delivery."""
import json
from pathlib import Path

import pytest

from src.dapier.api.designer_store import parse_workflow
from src.dapier.connectors.webhook import HttpError, run_http_request
from src.dapier.engine import logic_pkg as logic
from src.dapier.engine.actions.code import run_code


ROOT = Path(__file__).resolve().parents[1]


def workflow():
    return parse_workflow((ROOT / "workflows/aisl-recording-agent.yaml").read_text())


def event(kind):
    return {"id": "zoom:stable-delivery", "connector": "zoom", "event": kind,
            "occurred_at": "2026-11-25T10:20:00Z", "data": {
                "available_at": "2026-11-25T10:00:00Z",
                "meeting_id": 123, "meeting_uuid": 'instance/+"quoted',
                "start_time": "2026-11-25T09:00:00Z", "video_files": []}}


@pytest.mark.parametrize("kind", ["recording.completed", "recording.transcript_completed"])
def test_aisl_delivery_precedes_llm_and_cannot_launch_publishing_agent(kind):
    flow = workflow()
    requests = []
    ran = []

    def transport(method, url, **kwargs):
        requests.append((method, url, kwargs))
        return 202, b'{"matched":true,"event_id":69,"stage":"accepted"}'

    def runner(action, delivery, workflow_id, steps=None):
        ran.append(action["id"])
        if action["type"] == "storage_get":
            return {"value": "Token fixture-only"}
        if action["type"] == "code":
            return run_code(action, delivery, steps=steps)
        if action["type"] == "http_request":
            return run_http_request(action, delivery, steps=steps, transport=transport)
        pytest.fail("An accepted AISL recording must not reach a generic agent")

    logic.run_chain(flow["id"], flow["actions"], event(kind), runner)
    assert ran == ["aisl-authorization", "aisl-payload", "aisl-intake", "aisl-response"]
    method, url, request = requests[0]
    assert method == "POST"
    assert url == "https://aishippinglabs.com/api/recording-intake"
    assert request["headers"]["authorization"] == "Token fixture-only"
    assert json.loads(request["body"]) == {
        "event": kind, "available_at": "2026-11-25T10:00:00Z",
        "meeting_id": "123", "recording_uuid": 'instance/+"quoted',
        "start_time": "2026-11-25T09:00:00Z"}


def test_ambiguous_or_failed_intake_does_not_fall_through_to_llm():
    flow = workflow()
    ran = []

    def runner(action, delivery, workflow_id, steps=None):
        ran.append(action["id"])
        if action["type"] == "storage_get":
            return {"value": "Token fixture-only"}
        if action["type"] == "code":
            return run_code(action, delivery, steps=steps)
        return run_http_request(action, delivery, steps=steps,
                                transport=lambda *args, **kwargs: (409, b'{}'))

    errors = []
    with pytest.raises(HttpError, match="HTTP 409"):
        logic.run_chain(flow["id"], flow["actions"], event("recording.completed"), runner,
                        on_action_error=lambda *args, **kwargs: errors.append(args[3]))
    assert ran == ["aisl-authorization", "aisl-payload", "aisl-intake"]
    assert len(errors) == 1
    assert isinstance(errors[0], HttpError)
    assert errors[0].status == 409


def test_unmatched_recording_keeps_one_on_one_route_only():
    flow = workflow()
    route = flow["actions"][-1]
    classifier = route["default"][0]
    assert classifier["choices"] == ["one-on-one", "otherwise"]
    cases = {case["choice"]: case for case in classifier["cases"]}
    assert cases["otherwise"]["actions"] == []
    session = cases["one-on-one"]["actions"][0]
    assert session["type"] == "hub_session"
    assert session["workspace"] == "zoom-calls"


@pytest.mark.parametrize("choice,expected", [("one-on-one", ["process-call"]), ("otherwise", [])])
def test_unmatched_recording_executes_existing_generic_branch(monkeypatch, choice, expected):
    from src.dapier.connectors import ai
    monkeypatch.setattr(ai, "openrouter_credential", lambda: {"api_key": "fixture-only"})
    monkeypatch.setattr(ai, "run_decisions", lambda **kwargs: {"ok": True, "choice": choice})
    flow = workflow()
    sessions = []

    def runner(action, delivery, workflow_id, steps=None):
        if action["type"] == "storage_get":
            return {"value": "Token fixture-only"}
        if action["type"] == "code":
            return run_code(action, delivery, steps=steps)
        if action["type"] == "http_request":
            return run_http_request(action, delivery, steps=steps,
                                    transport=lambda *args, **kwargs: (200, b'{"matched":false}'))
        if action["type"] == "hub_session":
            sessions.append(action["id"])
            return {"queued": True}
        pytest.fail("Unexpected external action")

    logic.run_chain(flow["id"], flow["actions"], event("recording.completed"), runner)
    assert sessions == expected


def test_malformed_success_response_fails_closed_before_generic_processing():
    flow = workflow()
    ran = []

    def runner(action, delivery, workflow_id, steps=None):
        ran.append(action["id"])
        if action["type"] == "storage_get":
            return {"value": "Token fixture-only"}
        if action["type"] == "code":
            return run_code(action, delivery, steps=steps)
        if action["type"] == "http_request":
            return run_http_request(action, delivery, steps=steps,
                                    transport=lambda *args, **kwargs: (200, b'{}'))
        pytest.fail("Malformed response must not reach another action")

    with pytest.raises(RuntimeError, match="definitive matched boolean"):
        logic.run_chain(flow["id"], flow["actions"], event("recording.completed"), runner)
    assert ran[-1] == "aisl-response"
