import copy
import json

import boto3
import pytest
from moto import mock_aws

from plugins.hub.session import (
    DynamoJournal, HubSessionError, build_request, launch, request_key,
)

TASK = "10000000-0000-4000-8000-000000000001"
SESSION = "20000000-0000-4000-8000-000000000002"
ACTION = {"id": "delegate", "title": "Handle {subject}",
          "text": "Do {task}", "acceptance_criteria": "Deliver {task}"}
EVENT = {"id": "email/123", "data": {"subject": "request", "task": "the work"}}


@pytest.fixture
def journal():
    with mock_aws():
        table = boto3.resource("dynamodb", region_name="eu-west-1").create_table(
            TableName="workflow-storage",
            KeySchema=[{"AttributeName": "scope", "KeyType": "HASH"},
                       {"AttributeName": "key", "KeyType": "RANGE"}],
            AttributeDefinitions=[{"AttributeName": n, "AttributeType": "S"}
                                  for n in ("scope", "key")],
            BillingMode="PAY_PER_REQUEST")
        yield DynamoJournal(table, "flow")


def reply(path, payload):
    if not path:
        return 201, {"task": {"id": TASK, "version": 1}}
    return 200, {"assigned_session": {"hub_session_id": SESSION, "state": "starting",
                                     "fleet_launch_id": "launch-1"}}


def test_rendered_contract():
    request = build_request({**ACTION, "cwd": "notes", "tier": "deep"}, EVENT, "flow")
    assert request["create"]["title"] == "Handle request"
    assert request["create"]["text"] == "Do the work"
    assert request["create"]["acceptance_criteria"] == "Deliver the work"
    assert request["create"]["project"] == "dapier-agents"
    assert request["session"] == {
        "workspace": "dapier-agents", "cwd": "notes", "tier": "deep", "mode": "headless",
        "brief": "Handle request\n\nDo the work\n\nAcceptance criteria:\nDeliver the work"}
    assert "engine" not in json.dumps(request)


@pytest.mark.parametrize("changes", [
    {"acceptance_criteria": " "}, {"acceptance_criteria": "{data.missing}"},
    {"acceptance_criteria": None}, {"id": ""}, {"engine": "codex"},
    {"cwd": "../escape"}, {"cwd": "/etc"}, {"cwd": "..\\escape"},
    {"tier": "custom"}, {"title": "é" * 129}, {"project": "invalid project"},
    {"text": "x" * 8100}, {"attachments": [{"key": "attachment"}]},
    {"text": "x" * 7900, "acceptance_criteria": "y" * 300},
])
def test_invalid_input_never_calls_hub(journal, changes):
    def no_http(*args):
        pytest.fail("invalid request reached Hub")
    with pytest.raises(ValueError):
        launch({**ACTION, **changes}, EVENT, "flow", journal=journal, post=no_http)


def test_identity_is_stable_and_unambiguous():
    assert request_key("a:b", "c", "d") != request_key("a", "b:c", "d")
    assert request_key("flow", "event", "step") == request_key("flow", "event", "step")
    for part in range(3):
        identity = ["flow", "event", "step"]
        identity[part] = ""
        with pytest.raises(ValueError):
            request_key(*identity)


def test_lost_assignment_response_replays_snapshot_and_original_version(journal):
    calls = []
    attempt = 0

    def post(path, payload):
        nonlocal attempt
        calls.append((path, copy.deepcopy(payload)))
        # Each mutation must already be journaled before HTTP is attempted.
        rows = journal.table.scan()["Items"]
        assert any(json.loads(row["value"]).get("create") == payload for row in rows) if not path else any(
            json.loads(row["value"]).get("payload") == payload for row in rows)
        if path and attempt == 0:
            attempt += 1
            raise TimeoutError("response lost after launch")
        if not path and attempt:
            # Even an unexpected later create version cannot change assign CAS.
            return 200, {"task": {"id": TASK, "version": 9}}
        return reply(path, payload)

    with pytest.raises(TimeoutError):
        launch(ACTION, EVENT, "flow", journal=journal, post=post)
    result = launch({**ACTION, "text": None, "acceptance_criteria": ""}, EVENT, "flow", journal=journal, post=post)
    assert calls[0] == calls[2]
    assert calls[1] == calls[3]
    assert calls[3][1]["expected_version"] == 1
    assert result == {"hub_task_id": TASK, "hub_session_id": SESSION,
                      "hub_link": f"https://hub.dtcdev.click/#/intake/{TASK}",
                      "state": "starting", "fleet_launch_id": "launch-1"}
    assert all("expires" not in row for row in journal.table.scan()["Items"])


def test_lost_create_response_reuses_identical_request(journal):
    calls = []

    def post(path, payload):
        calls.append(copy.deepcopy(payload))
        if len(calls) == 1:
            raise TimeoutError("create response lost")
        return reply(path, payload)

    with pytest.raises(TimeoutError):
        launch(ACTION, EVENT, "flow", journal=journal, post=post)
    launch({**ACTION, "title": "changed"}, EVENT, "flow", journal=journal, post=post)
    assert calls[0] == calls[1]


def test_journal_first_writer_wins_and_scopes_are_isolated(journal):
    assert journal.once("key", {"a": 1}) == {"a": 1}
    assert journal.once("key", {"a": 2}) == {"a": 1}
    other = DynamoJournal(journal.table, "other-flow")
    assert other.once("key", {"a": 3}) == {"a": 3}


@pytest.mark.parametrize("status,body", [
    (409, {"error": "scope_decision_required"}),
    (403, {"error": "forbidden", "secret": "sensitive-response"}),
    (500, "sensitive-response"), (200, {}),
    (200, {"assigned_session": {"hub_session_id": "not-a-uuid"}}),
])
def test_assignment_failure_is_never_success_and_never_leaks_body(journal, status, body):
    def post(path, payload):
        return (status, body) if path else reply(path, payload)
    with pytest.raises(HubSessionError) as exc:
        launch(ACTION, EVENT, "flow", journal=journal, post=post)
    assert "sensitive-response" not in str(exc.value)


def test_held_result_preserves_reason_without_claiming_running(journal):
    def post(path, payload):
        if not path:
            return reply(path, payload)
        return 200, {"assigned_session": {"hub_session_id": SESSION, "state": "held"},
                     "hold": {"rejected": [{"reason": "needs_unmet"}]}}
    result = launch(ACTION, EVENT, "flow", journal=journal, post=post)
    assert result["state"] == "held"
    assert result["hold"]["rejected"][0]["reason"] == "needs_unmet"


def test_journal_access_failure_propagates():
    from botocore.exceptions import ClientError

    class DeniedTable:
        def get_item(self, **kwargs):
            return {}

        def put_item(self, **kwargs):
            raise ClientError({"Error": {"Code": "AccessDeniedException"}}, "PutItem")
    with pytest.raises(ClientError):
        DynamoJournal(DeniedTable(), "flow").once("key", {})


def test_title_boundary_and_step_output():
    request = build_request({**ACTION, "title": "é" * 128,
                             "text": "{steps.previous.output.task}"}, EVENT, "flow",
                            {"previous": {"output": {"task": "returned task"}}})
    assert request["create"]["title"] == "é" * 128
    assert request["create"]["text"] == "returned task"


def test_persisted_result_recovered_without_hub(journal):
    expected = launch(ACTION, EVENT, "flow", journal=journal, post=reply)

    def unavailable(*args):
        pytest.fail("persisted result must not require Hub access")
    assert launch({"id": "delegate"}, EVENT, "flow", journal=journal, post=unavailable) == expected


def test_concurrent_journal_insert_recovers_winning_snapshot(journal, monkeypatch):
    from botocore.exceptions import ClientError

    table = journal.table
    original_put = table.put_item

    def put_item(**kwargs):
        original_put(Item={**kwargs["Item"], "value": json.dumps({"winner": True})})
        raise ClientError({"Error": {"Code": "ConditionalCheckFailedException"}}, "PutItem")

    monkeypatch.setattr(table, "put_item", put_item)
    assert journal.once("raced", lambda: {"winner": False}) == {"winner": True}
