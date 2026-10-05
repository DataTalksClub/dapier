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

    def delete_item(self, **kwargs):
        self._calls.append(("delete_item", kwargs))


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


def test_operator_recipient_prefers_the_notify_inbox(monkeypatch):
    monkeypatch.setenv("DAPIER_NOTIFY_EMAIL", "ops@example.test")
    monkeypatch.setenv("BACKUP_ALERT_EMAIL", "backup@example.test")
    monkeypatch.setenv("DAPIER_EMAIL_SENDER", "no-reply@dapier.example.test")

    assert notify.operator_recipient() == "ops@example.test"
    assert notify.operator_sender() == "no-reply@dapier.example.test"


def test_operator_recipient_falls_back_to_the_backup_alert(monkeypatch):
    monkeypatch.delenv("DAPIER_NOTIFY_EMAIL", raising=False)
    monkeypatch.setenv("BACKUP_ALERT_EMAIL", "backup@example.test")
    monkeypatch.setenv("DAPIER_EMAIL_SENDER", "no-reply@dapier.example.test")

    assert notify.operator_recipient() == "backup@example.test"


def test_operator_recipient_falls_back_to_the_configured_sender(monkeypatch):
    monkeypatch.delenv("DAPIER_NOTIFY_EMAIL", raising=False)
    monkeypatch.delenv("BACKUP_ALERT_EMAIL", raising=False)
    monkeypatch.setenv("DAPIER_EMAIL_SENDER", "no-reply@dapier.example.test")
    monkeypatch.setenv("TRIGGER_EMAIL_DOMAIN", "other.example.test")

    assert notify.operator_recipient() == "no-reply@dapier.example.test"


def test_operator_recipient_falls_back_to_the_trigger_domain(monkeypatch):
    monkeypatch.delenv("DAPIER_NOTIFY_EMAIL", raising=False)
    monkeypatch.delenv("BACKUP_ALERT_EMAIL", raising=False)
    monkeypatch.delenv("DAPIER_EMAIL_SENDER", raising=False)
    monkeypatch.setenv("TRIGGER_EMAIL_DOMAIN", "ops.example.test")

    assert notify.operator_recipient() == "no-reply@ops.example.test"


def test_ses_client_uses_the_email_send_region(monkeypatch):
    seen = {}

    def fake_client(service, region_name=None):
        seen["service"] = service
        seen["region_name"] = region_name
        return FakeSes()

    monkeypatch.setenv("DAPIER_EMAIL_REGION", "us-east-1")
    monkeypatch.setattr("boto3.client", fake_client)

    client = notify.ses_client()

    assert seen == {"service": "ses", "region_name": "us-east-1"}
    assert isinstance(client, FakeSes)


def test_missing_notify_defaults_to_the_operator_address(monkeypatch):
    import src.dapier.engine.matching as matching

    monkeypatch.setattr(matching, "all_workflows", lambda: [{"id": "wf-1"}])
    monkeypatch.delenv("DAPIER_NOTIFY_EMAIL", raising=False)
    monkeypatch.delenv("BACKUP_ALERT_EMAIL", raising=False)
    monkeypatch.setenv("DAPIER_EMAIL_SENDER", "ops@example.test")

    assert notify.notify_addresses("wf-1") == ["ops@example.test"]


def test_malformed_notify_defaults_to_the_operator_address(monkeypatch):
    import src.dapier.engine.matching as matching

    monkeypatch.setattr(matching, "all_workflows",
                        lambda: [{"id": "wf-1", "notify": {"oops": True}}])
    monkeypatch.delenv("DAPIER_NOTIFY_EMAIL", raising=False)
    monkeypatch.delenv("BACKUP_ALERT_EMAIL", raising=False)
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
    monkeypatch.delenv("DAPIER_NOTIFY_EMAIL", raising=False)
    monkeypatch.delenv("BACKUP_ALERT_EMAIL", raising=False)
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


def test_notify_failure_sends_to_the_notify_inbox_from_the_sender(monkeypatch):
    calls = []
    _patch_table(monkeypatch, calls)
    import src.dapier.engine.matching as matching

    monkeypatch.setattr(matching, "all_workflows", lambda: [{"id": "wf-1"}])
    monkeypatch.setenv("DAPIER_NOTIFY_EMAIL", "ops@example.test")
    monkeypatch.setenv("DAPIER_EMAIL_SENDER", "no-reply@dtcdev.click")
    monkeypatch.setenv("HOOKS_BASE_URL", "https://dapier.example.test")
    ses = FakeSes()
    exc = ValueError("boom")
    exc.dapier_workflow = "wf-1"

    result = notify.notify_failure(exc, {"id": "evt-1", "connector": "email",
                                         "event": "message.received"}, ses=ses)

    assert result["to"] == ["ops@example.test"]
    sent = ses.calls[0]
    assert sent["Source"] == "no-reply@dtcdev.click"
    assert sent["Destination"]["ToAddresses"] == ["ops@example.test"]
    body = sent["Message"]["Body"]["Text"]["Data"]
    assert "console: https://dapier.example.test/runs" in body


class _FailingSes:
    def send_email(self, **kwargs):
        raise RuntimeError("SES identity is in another region")


def test_notify_failure_releases_the_claim_when_ses_fails(monkeypatch):
    calls = []
    _patch_table(monkeypatch, calls)
    import src.dapier.engine.matching as matching

    monkeypatch.setattr(matching, "all_workflows", lambda: [{"id": "wf-1"}])
    monkeypatch.setenv("DAPIER_NOTIFY_EMAIL", "ops@example.test")
    monkeypatch.setenv("DAPIER_EMAIL_SENDER", "no-reply@dtcdev.click")
    exc = ValueError("boom")
    exc.dapier_workflow = "wf-1"
    event = {"id": "evt-1"}

    assert notify.notify_failure(exc, event, ses=_FailingSes()) is None
    assert [name for name, _kwargs in calls] == ["put_item", "delete_item"]
    assert calls[1][1]["Key"] == {"execution_id": "wf-1:failure-notice:evt-1"}


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
