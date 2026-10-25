"""Sender refusals page once per mailbox/sender without recursive notices."""

import json
import logging
from functools import partial
from pathlib import Path
from unittest.mock import Mock

import boto3
import pytest
import yaml
from moto import mock_aws

from src.dapier import engine
from src.dapier.engine import worker
from src.dapier.triggers import email_from, email_refusals


@pytest.fixture
def signal_store(monkeypatch):
    monkeypatch.setenv("TRIGGER_EMAIL_DOMAIN", "dtcdev.click")
    monkeypatch.setenv("DAPIER_EMAIL_SENDER", "no-reply@dtcdev.click")
    monkeypatch.setattr(engine, "workflows", lambda: [{
        "id": "dev-mailbox", "enabled": True,
        "trigger": {"connector": "email", "event": "message.received",
                    "filters": {"route": {"equals": "dev"},
                                "subject": {"prefix": "alert"}}},
    }])
    with mock_aws():
        table = boto3.resource("dynamodb", region_name="eu-west-1").create_table(
            TableName="cursors", BillingMode="PAY_PER_REQUEST",
            KeySchema=[{"AttributeName": "cursor_id", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "cursor_id", "AttributeType": "S"}],
        )
        cloudwatch = Mock()
        yield table, cloudwatch


def mail(sender="noreply@datatalks.club", route="dev", subject="alert"):
    return {"id": "email-1", "connector": "email", "event": "message.received",
            "data": {"route": route, "from": sender, "subject": subject}}


def test_worker_refusal_logs_error_and_signals_enabled_mailbox(monkeypatch, signal_store, caplog):
    table, cloudwatch = signal_store
    monkeypatch.setenv("EMAIL_FROM_TABLE", "senders")
    # Exercise the real sender gate with the inherited default allowlist.
    sender_table = Mock()
    sender_table.get_item.return_value = {"Item": {"mode": "allowlist",
                                                  "addresses": list(email_from.SEED)}}
    monkeypatch.setattr(email_from, "_table", lambda _: sender_table)
    monkeypatch.setattr(worker, "_note_email_event", Mock())
    monkeypatch.setattr(worker.inbox, "record", lambda _: "inbox-1")
    ignore = Mock()
    monkeypatch.setattr(worker.inbox, "ignore", ignore)
    execute = Mock(side_effect=AssertionError("refused mail must never execute"))
    monkeypatch.setattr(worker, "execute", execute)
    monkeypatch.setattr(email_refusals, "signal", partial(
        email_refusals.signal, table_ref=table, cloudwatch=cloudwatch, now=100))
    # Even a subject filter that would not match must not hide the refusal.
    event = mail(subject="other subject")
    with caplog.at_level(logging.ERROR):
        result = worker.handler({"Records": [{"messageId": "sqs-1",
                                              "body": json.dumps(event)}]}, None)
    assert result == {"batchItemFailures": []}
    ignore.assert_called_once_with("inbox-1", error="sender not allowed")
    execute.assert_not_called()
    assert "dev@dtcdev.click" in caplog.text
    assert "noreply@datatalks.club" in caplog.text
    assert "sender not allowed" in caplog.text
    assert "rule=sender_not_in_allowlist event_id=email-1 run_id=not-started" in caplog.text
    cloudwatch.put_metric_data.assert_called_once_with(
        Namespace="Dapier/Triggers", MetricData=[{
            "MetricName": "EmailSenderPolicyRefused", "Value": 1, "Unit": "Count"}])


def test_atomic_dedupe_is_per_address_sender_and_expires_without_ttl_cleanup(signal_store):
    table, cloudwatch = signal_store
    signal = partial(email_refusals.signal, table_ref=table, cloudwatch=cloudwatch)
    assert signal(mail(), now=100) == 1
    # A different event id / display name is the same mailbox-sender pair.
    duplicate = mail("Alerts <NOREPLY@datatalks.club>")
    duplicate["id"] = "email-2"
    assert signal(duplicate, now=101) == 0
    assert signal(mail(), now=86499) == 0
    assert signal(mail("no-reply@dtcdev.click"), now=102) == 1
    # Expired DynamoDB items may still exist; the conditional put renews them.
    assert signal(mail(), now=86500) == 1
    assert cloudwatch.put_metric_data.call_count == 3


@pytest.mark.parametrize("workflow_state", [None, {"enabled": False}, {"auto_paused": True}])
def test_no_signal_without_enabled_mailbox(monkeypatch, signal_store, workflow_state, caplog):
    table, cloudwatch = signal_store
    workflows = engine.workflows()
    if workflow_state is None:
        workflows = []
    else:
        workflows[0].update(workflow_state)
    monkeypatch.setattr(engine, "workflows", lambda: workflows)
    assert email_refusals.signal(mail(), table_ref=table, cloudwatch=cloudwatch) == 0
    cloudwatch.put_metric_data.assert_not_called()
    assert table.scan()["Items"] == []
    assert not caplog.records


def test_distinct_mailboxes_have_distinct_claims(monkeypatch, signal_store):
    table, cloudwatch = signal_store
    workflows = engine.workflows()
    workflows.append({"id": "tofu-mailbox", "trigger": {
        "connector": "email", "event": "message.received",
        "filters": {"route": {"equals": "tofu"}}}})
    monkeypatch.setattr(engine, "workflows", lambda: workflows)
    for route in ("dev", "tofu"):
        assert email_refusals.signal(mail(route=route), table_ref=table,
                                     cloudwatch=cloudwatch, now=100) == 1
    assert table.scan()["Count"] == 2


@pytest.mark.parametrize("sender", [
    "no-reply@dtcdev.click",
    "alerts@dtcdev.click",
    "DTC Alerts <alerts@dtcdev.click>",
])
@pytest.mark.parametrize("subject", [
    "[dapier] alarm: refusal-alarm", "dapier error digest: 1 failed run (last 24h)",
    "[dapier] Run failed: dev-mailbox", "[dapier] Workflow auto-paused: dev-mailbox",
    "[dapier] 2 alarms fired",
])
def test_refused_operator_notices_cannot_retrigger_alarm(signal_store, caplog, sender, subject):
    table, cloudwatch = signal_store
    with caplog.at_level(logging.ERROR):
        for now in (100, 86500, 172900):
            assert email_refusals.signal(mail(sender, subject=subject),
                                         table_ref=table, cloudwatch=cloudwatch, now=now) == 0
    assert len(caplog.records) == 3  # Still visible in logs.
    cloudwatch.put_metric_data.assert_not_called()
    assert table.scan()["Items"] == []


def test_metric_failure_releases_claim_for_retry(signal_store):
    table, cloudwatch = signal_store
    cloudwatch.put_metric_data.side_effect = RuntimeError("metric unavailable")
    with pytest.raises(RuntimeError, match="metric unavailable"):
        email_refusals.signal(mail(), table_ref=table, cloudwatch=cloudwatch, now=100)
    assert table.scan()["Items"] == []
    cloudwatch.put_metric_data.side_effect = None
    assert email_refusals.signal(mail(), table_ref=table, cloudwatch=cloudwatch, now=101) == 1


def test_refusal_alarm_uses_existing_operator_topic():
    class Loader(yaml.SafeLoader):
        pass

    def intrinsic(loader, tag, node):
        if isinstance(node, yaml.SequenceNode):
            return loader.construct_sequence(node)
        if isinstance(node, yaml.MappingNode):
            return loader.construct_mapping(node)
        return loader.construct_scalar(node)

    Loader.add_multi_constructor("!", intrinsic)
    template = yaml.load((Path(__file__).parents[1] / "template.yaml").read_text(), Loader=Loader)
    alarm = template["Resources"]["EmailSenderPolicyRefusalAlarm"]["Properties"]
    assert alarm["MetricName"] == email_refusals.METRIC
    assert alarm["Namespace"] == "Dapier/Triggers"
    assert alarm["AlarmActions"] == ["AlarmTopic"]
    assert alarm["Period"] * alarm["EvaluationPeriods"] == 86400
    assert alarm["DatapointsToAlarm"] == 1
    assert alarm["TreatMissingData"] == "notBreaching"
    policies = template["Resources"]["AlarmNotifyFunction"]["Properties"]["Policies"]
    assert {"DynamoDBReadPolicy": {"TableName": "TriggerInboxTable"}} in policies
    assert {"DynamoDBReadPolicy": {"TableName": "PublishedWorkflowsTable"}} in policies


def test_alarm_context_reads_paginated_envelopes_and_excludes_content(monkeypatch, signal_store):
    from src.dapier.triggers import inbox

    with mock_aws():
        table = boto3.resource("dynamodb", region_name="eu-west-1").create_table(
            TableName="inbox", BillingMode="PAY_PER_REQUEST",
            KeySchema=[{"AttributeName": "inbox_id", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "inbox_id", "AttributeType": "S"}],
        )
        for event_id, route, sender, at, subject in [
            ("refused-1", "dev", "outsider@example.test", "2026-10-25T12:14:47+00:00", "private subject"),
            ("missing-sender", "dev", "", "2026-10-25T12:13:00+00:00", "private subject"),
            ("old", "dev", "outsider@example.test", "2026-10-24T12:14:47+00:00", "private subject"),
            ("later", "dev", "outsider@example.test", "2026-10-25T12:16:00+00:00", "private subject"),
            ("unclaimed", "unknown", "outsider@example.test", "2026-10-25T12:14:47+00:00", "private subject"),
            ("operator-loop", "dev", "alerts@dtcdev.click", "2026-10-25T12:14:47+00:00", "[dapier] alarm: X"),
        ]:
            table.put_item(Item={
                "inbox_id": event_id, "connector": "email", "status": inbox.IGNORED,
                "error": "sender not allowed", "processed_at": at,
                "email": {"route": route, "from_address": sender, "subject": subject,
                          "text": "private body", "html": "private html", "attachments": ["private file"]},
                "data": {"secret": "must not be loaded"},
            })
        requests = []
        class Paginated:
            def scan(self, **kwargs):
                requests.append(kwargs)
                return table.scan(**kwargs, Limit=1)
        details = email_refusals.alarm_details("2026-10-25T14:15:00+02:00", table_ref=Paginated())
    assert len(requests) > 1
    assert "ExclusiveStartKey" in requests[1]
    assert "recipient: dev@dtcdev.click" in details
    assert "sender: outsider@example.test" in details
    assert "workflow: dev-mailbox" in details
    assert "rule: sender_not_in_allowlist" in details
    assert "rule: missing_sender" in details
    assert "inbox/event id: refused-1" in details
    assert "run id: not started" in details
    for private in ("private subject", "private body", "private html", "private file", "must not be loaded",
                    "inbox/event id: old", "inbox/event id: later", "unclaimed", "operator-loop"):
        assert private not in details
    for request in requests:
        assert "data" not in request["ProjectionExpression"]
        assert "text" not in request["ProjectionExpression"]
        assert "html" not in request["ProjectionExpression"]
        assert "attachments" not in request["ProjectionExpression"]


def test_alarm_context_is_bounded_and_explains_missing_context(signal_store):
    table = Mock()
    table.scan.return_value = {"Items": []}
    assert "No retained refusal context" in email_refusals.alarm_details(table_ref=table)
    table.scan.return_value = {"Items": [{
        "inbox_id": f"event-{i}", "processed_at": "2026-10-25T12:14:47+00:00",
        "email": {"route": "dev", "from": "outsider@example.test"},
    } for i in range(21)]}
    details = email_refusals.alarm_details("2026-10-25T12:15:00Z", table_ref=table)
    assert details.count("recipient:") == 20
    assert "1 additional refusals" in details
