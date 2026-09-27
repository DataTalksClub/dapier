"""Failure notification defaults: the operator recipient and the poll-ingress path.

Default-on error visibility (G7): a workflow with no ``notify:`` notifies
the operator address, an explicit ``notify: []`` opts out, and failures
with no workflow behind them (a poll trigger the worker could not run)
notify the operator too.
"""
import boto3
import pytest

from src.dapier.engine import notify


class FakeSes:
    def __init__(self):
        self.calls = []

    def send_email(self, **kwargs):
        self.calls.append(kwargs)
        return {"MessageId": "ses-1"}


class _CapturingTable:
    def __init__(self, calls):
        self._calls = calls

    def put_item(self, **kwargs):
        self._calls.append(("put_item", kwargs))


def _patch_table(monkeypatch, calls):
    monkeypatch.setenv("EXECUTIONS_TABLE", "executions")

    class Dynamo:
        def Table(self, _name):
            return _CapturingTable(calls)

    monkeypatch.setattr(boto3, "resource", lambda service: Dynamo())


def _tagged(workflow_id="wf-1"):
    exc = ValueError("Slack rejected message")
    exc.dapier_workflow = workflow_id
    return exc


def test_operator_recipient_prefers_the_configured_sender(monkeypatch):
    monkeypatch.setenv("DAPIER_EMAIL_SENDER", "no-reply@dapier.example.test")
    monkeypatch.setenv("TRIGGER_EMAIL_DOMAIN", "other.example.test")

    assert notify.operator_recipient() == "no-reply@dapier.example.test"


def test_operator_recipient_falls_back_to_the_trigger_domain(monkeypatch):
    monkeypatch.delenv("DAPIER_EMAIL_SENDER", raising=False)
    monkeypatch.setenv("TRIGGER_EMAIL_DOMAIN", "ops.example.test")

    assert notify.operator_recipient() == "no-reply@ops.example.test"


def test_missing_notify_defaults_to_the_operator_address(monkeypatch):
    import src.dapier.engine.matching as matching

    monkeypatch.setattr(matching, "all_workflows", lambda: [{"id": "wf-1"}])
    monkeypatch.setenv("DAPIER_EMAIL_SENDER", "ops@example.test")

    assert notify.notify_addresses("wf-1") == ["ops@example.test"]


def test_malformed_notify_defaults_to_the_operator_address(monkeypatch):
    import src.dapier.engine.matching as matching

    monkeypatch.setattr(matching, "all_workflows",
                        lambda: [{"id": "wf-1", "notify": {"oops": True}}])
    monkeypatch.setenv("DAPIER_EMAIL_SENDER", "ops@example.test")

    assert notify.notify_addresses("wf-1") == ["ops@example.test"]


def test_explicit_empty_notify_opts_out(monkeypatch):
    import src.dapier.engine.matching as matching

    monkeypatch.setattr(matching, "all_workflows",
                        lambda: [{"id": "wf-1", "notify": []}])
    monkeypatch.setenv("DAPIER_EMAIL_SENDER", "ops@example.test")

    assert notify.notify_addresses("wf-1") == []


def test_unknown_workflow_stays_quiet(monkeypatch):
    import src.dapier.engine.matching as matching

    monkeypatch.setattr(matching, "all_workflows", lambda: [])
    monkeypatch.setenv("DAPIER_EMAIL_SENDER", "ops@example.test")

    assert notify.notify_addresses("wf-missing") == []


def test_poll_failure_without_a_workflow_notifies_the_operator(monkeypatch):
    calls = []
    _patch_table(monkeypatch, calls)
    monkeypatch.delenv("DAPIER_EMAIL_SENDER", raising=False)
    monkeypatch.setenv("TRIGGER_EMAIL_DOMAIN", "ops.example.test")
    ses = FakeSes()
    # No dapier_workflow tag: the fire failed before any workflow matched.
    event = {
        "id": "3f2a9c",
        "connector": "poll",
        "event": "poll.failed",
        "data": {"poll": "new-items"},
    }

    result = notify.notify_failure(RuntimeError("fetch failed"), event, ses=ses)

    assert result["to"] == ["no-reply@ops.example.test"]
    assert result["run_id"] == "poll:new-items:3f2a9c"
    sent = ses.calls[0]
    assert sent["Source"] == "no-reply@ops.example.test"
    assert "[dapier] Run failed: poll:new-items" in sent["Message"]["Subject"]["Data"]
    body = sent["Message"]["Body"]["Text"]["Data"]
    assert "workflow: poll:new-items" in body
    assert "trigger: poll / poll.failed" in body
    assert "failing step error: fetch failed" in body
    item = calls[0][1]["Item"]
    assert item["execution_id"] == "poll:new-items:failure-notice:3f2a9c"
    assert item["workflow_id"] == "poll:new-items"
    assert item["kind"] == "failure-notice"


def test_untagged_non_poll_events_stay_declined(monkeypatch):
    calls = []
    _patch_table(monkeypatch, calls)
    monkeypatch.setenv("DAPIER_EMAIL_SENDER", "ops@example.test")
    ses = FakeSes()

    # A queue record that never matched a workflow (garbage payload) is not
    # a poll failure: the DLQ/worker alarms cover that volume.
    assert notify.notify_failure(
        ValueError("boom"), {"id": "evt-1", "connector": "email"}, ses=ses) is None
    assert ses.calls == []
    assert calls == []


def test_non_dict_events_stay_declined(monkeypatch):
    calls = []
    _patch_table(monkeypatch, calls)
    monkeypatch.setenv("DAPIER_EMAIL_SENDER", "ops@example.test")

    assert notify.notify_failure(_tagged(), None, ses=FakeSes()) is None


def test_opted_out_workflow_failure_sends_nothing(monkeypatch):
    import src.dapier.engine.matching as matching

    calls = []
    _patch_table(monkeypatch, calls)
    monkeypatch.setattr(matching, "all_workflows",
                        lambda: [{"id": "wf-1", "notify": []}])
    monkeypatch.setenv("DAPIER_EMAIL_SENDER", "ops@example.test")
    ses = FakeSes()

    assert notify.notify_failure(_tagged(), {"id": "evt-1"}, ses=ses) is None
    assert ses.calls == []
    assert calls == []


if __name__ == "__main__":
    pytest.main([__file__])
