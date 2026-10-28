"""Provenance gate: hand-built queue events never page the refusal alarm.

A synthetic inbound-email event sent straight to the live EventQueue (the
DataOps bfd720b8 shape: TEST id, no S3/raw_mime pointer) must take the
explicit invalid-provenance path: recorded as ignored, logged once at
WARNING, no workflow runs, and EmailSenderPolicyRefused is never emitted.
Authenticated intake/replay/retry deliveries and signed SNS receipts retain
sender-policy behavior. Payload pointers cannot establish trust.
"""

import json
import logging
from functools import partial
from unittest.mock import Mock

import boto3
import pytest
from moto import mock_aws

from src.dapier import engine
from src.dapier.engine import worker
from src.dapier.triggers import email_from, email_provenance, email_refusals


def test_provenance_uses_sqs_identity_not_payload(monkeypatch):
    monkeypatch.setenv("EMAIL_PUBLISHER_ROLE_IDS", "AROAINTAKE,AROAAPI,AROAWORKER")
    payload = {"connector": "email", "event": "message.received",
               "data": {"s3": {"bucket": "real-looking", "key": "raw/message"}}}
    assert not email_provenance.has_valid_provenance(payload, {})
    assert not email_provenance.has_valid_provenance(payload, {
        "attributes": {"SenderId": "AROAAGENT:session"},
        "messageAttributes": {"SenderId": "AROAINTAKE:session"}})
    for role in ("AROAINTAKE", "AROAAPI", "AROAWORKER"):
        assert email_provenance.has_valid_provenance(payload, {
            "attributes": {"SenderId": role + ":session"}})
    monkeypatch.delenv("EMAIL_PUBLISHER_ROLE_IDS")
    assert not email_provenance.has_valid_provenance(payload, {
        "attributes": {"SenderId": "AROAINTAKE:session"}})


@pytest.fixture
def mailbox(monkeypatch):
    monkeypatch.setenv("EMAIL_PUBLISHER_ROLE_IDS", "AROAINTAKE")
    monkeypatch.setenv("TRIGGER_EMAIL_DOMAIN", "dtcdev.click")
    monkeypatch.setenv("DAPIER_EMAIL_SENDER", "no-reply@dtcdev.click")
    monkeypatch.setattr(engine, "workflows", lambda: [{
        "id": "invoice-intake", "enabled": True,
        "trigger": {"connector": "email", "event": "message.received",
                    "filters": {"route": {"equals": "invoice"}}},
    }])
    with mock_aws():
        table = boto3.resource("dynamodb", region_name="eu-west-1").create_table(
            TableName="cursors", BillingMode="PAY_PER_REQUEST",
            KeySchema=[{"AttributeName": "cursor_id", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "cursor_id", "AttributeType": "S"}],
        )
        cloudwatch = Mock()
        yield table, cloudwatch


def _sender_gate(monkeypatch):
    monkeypatch.setenv("EMAIL_FROM_TABLE", "senders")
    sender_table = Mock()
    sender_table.get_item.return_value = {"Item": {"mode": "allowlist",
                                                  "addresses": list(email_from.SEED)}}
    monkeypatch.setattr(email_from, "_table", lambda _: sender_table)


@pytest.mark.parametrize("fake_pointer", [False, True])
def test_synthetic_event_takes_invalid_provenance_path(mailbox, monkeypatch, caplog, fake_pointer):
    _sender_gate(monkeypatch)
    monkeypatch.setattr(worker, "_note_email_event", Mock())
    monkeypatch.setattr(worker.inbox, "record", lambda _: "inbox-9")
    ignore = Mock()
    monkeypatch.setattr(worker.inbox, "ignore", ignore)
    execute = Mock(side_effect=AssertionError("synthetic mail must never execute"))
    monkeypatch.setattr(worker, "execute", execute)
    signal = Mock()
    monkeypatch.setattr(email_refusals, "signal", signal)

    synthetic = {
        "id": "TEST-replace-document-bfd720b8-20261025",
        "connector": "email", "event": "message.received",
        "source": "TEST-invoice-replacement-acceptance",
        "occurred_at": "2026-10-25T12:12:30Z",
        "data": {"route": "invoice",
                 "message_id": "TEST-replace-document-bfd720b8-20261025",
                 "sender": {"addresses": ["test-replace-document@example.test"]},
                 "subject": "TEST replace-document acceptance invoice",
                 "body": {"text": {"value": "synthetic"}},
                 "attachments": []},
    }
    if fake_pointer:
        synthetic["data"]["s3"] = {"bucket": "mail-bucket", "key": "raw/fake"}
    monkeypatch.setattr(worker, "normalize_payload", Mock(side_effect=AssertionError("must not normalize")))
    queue_arn = "arn:aws:sqs:eu-west-1:817685572750:events"
    monkeypatch.setenv("INBOUND_EMAIL_QUEUE_ARN", queue_arn)
    with caplog.at_level(logging.WARNING):
        result = worker.handler({"Records": [{"messageId": "sqs-9",
            "eventSourceARN": queue_arn,
            "attributes": {"SenderId": "AROAAGENT:synthetic-test"},
            "body": json.dumps(synthetic)}]}, None)
    assert result == {"batchItemFailures": []}
    ignore.assert_called_once_with(
        "inbox-9", error=email_provenance.INVALID_PROVENANCE_ERROR)
    execute.assert_not_called()
    signal.assert_not_called()
    assert "invalid provenance" in caplog.text
    assert "TEST-replace-document-bfd720b8-20261025" in caplog.text
    assert "sender not allowed" not in caplog.text


def test_genuine_non_allowlisted_sender_still_refused_and_counted(
        mailbox, monkeypatch, caplog):
    table, cloudwatch = mailbox
    _sender_gate(monkeypatch)
    monkeypatch.setattr(worker, "_note_email_event", Mock())
    monkeypatch.setattr(worker.inbox, "record", lambda _: "inbox-1")
    ignore = Mock()
    monkeypatch.setattr(worker.inbox, "ignore", ignore)
    execute = Mock(side_effect=AssertionError("refused mail must never execute"))
    monkeypatch.setattr(worker, "execute", execute)
    monkeypatch.setattr(email_refusals, "signal", partial(
        email_refusals.signal, table_ref=table, cloudwatch=cloudwatch, now=100))

    genuine = {"id": "email:genuine-1", "connector": "email",
               "event": "message.received", "source": "invoice",
               "data": {"route": "invoice", "from": "stranger@example.com",
                        "subject": "invoice",
                        "s3": {"bucket": "mail-bucket", "key": "raw/genuine-1"}}}
    with caplog.at_level(logging.ERROR):
        result = worker.handler({"Records": [{"messageId": "sqs-1", "attributes": {"SenderId": "AROAINTAKE:session"},
                                               "body": json.dumps(genuine)}]}, None)
    assert result == {"batchItemFailures": []}
    ignore.assert_called_once_with("inbox-1", error="sender not allowed")
    execute.assert_not_called()
    assert "sender not allowed" in caplog.text
    cloudwatch.put_metric_data.assert_called_once_with(
        Namespace="Dapier/Triggers", MetricData=[{
            "MetricName": "EmailSenderPolicyRefused", "Value": 1, "Unit": "Count"}])


@pytest.mark.parametrize("raw_delivery", [False, True])
def test_genuine_relay_sns_non_allowlisted_sender_still_refused(mailbox, monkeypatch, caplog, raw_delivery):
    table, cloudwatch = mailbox
    _sender_gate(monkeypatch)
    monkeypatch.setattr(worker, "_note_email_event", Mock())
    recorded = {}

    def _record(event):
        recorded["event"] = event
        return "inbox-sns"

    monkeypatch.setattr(worker.inbox, "record", _record)
    ignore = Mock()
    monkeypatch.setattr(worker.inbox, "ignore", ignore)
    execute = Mock(side_effect=AssertionError("refused mail must never execute"))
    monkeypatch.setattr(worker, "execute", execute)
    monkeypatch.setattr(email_refusals, "signal", partial(
        email_refusals.signal, table_ref=table, cloudwatch=cloudwatch, now=200))

    relay = {
        "contract": "inbound-email", "version": 1, "event_id": "sns-genuine-1",
        "occurred_at": "2026-10-26T00:00:00Z", "route": "invoice@dtcdev.click",
        "message_id": "<sns-genuine-1@example.com>",
        "sender": {"addresses": ["stranger@example.com"]},
        "recipients": {"matched": ["invoice@dtcdev.click"]},
        "subject": "invoice", "raw_mime": {"bucket": "b", "key": "raw/sns-1"},
    }
    caplog.set_level(logging.ERROR)
    record = {"messageId": "m1"}
    if raw_delivery:
        monkeypatch.setenv("INBOUND_EMAIL_QUEUE_ARN", "arn:aws:sqs:eu-west-1:817685572750:events")
        record["eventSourceARN"] = "arn:aws:sqs:eu-west-1:817685572750:events"
        envelope = relay
    else:
        envelope = signed_notification(relay, monkeypatch)
    record["body"] = json.dumps(envelope)
    result = worker.handler({"Records": [record]}, None)
    assert result == {"batchItemFailures": []}
    ignore.assert_called_once_with("inbox-sns", error="sender not allowed")
    execute.assert_not_called()
    assert cloudwatch.put_metric_data.call_count == 1
    assert "sender not allowed" in caplog.text


def signed_notification(message, monkeypatch, version="2"):
    import base64
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import padding, rsa
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    topic = "arn:aws:sns:eu-west-1:817685572750:relay-sandbox-inbound-email-events"
    monkeypatch.setenv("INBOUND_EMAIL_TOPICS", topic)
    envelope = {"Type": "Notification", "Message": json.dumps(message),
                "MessageId": "sns-1", "Timestamp": "2026-10-26T00:00:00Z", "TopicArn": topic,
                "SignatureVersion": version,
                "SigningCertURL": "https://sns.eu-west-1.amazonaws.com/SimpleNotificationService-test.pem"}
    canonical = "".join(f"{field}\n{envelope[field]}\n" for field in
                        ["Message", "MessageId", "Timestamp", "TopicArn", "Type"])
    algorithm = hashes.SHA256() if version == "2" else hashes.SHA1()
    envelope["Signature"] = base64.b64encode(key.sign(
        canonical.encode(), padding.PKCS1v15(), algorithm)).decode()
    monkeypatch.setattr(email_provenance, "_certificate", lambda url: key.public_key())
    return envelope


@pytest.mark.parametrize("version", ["1", "2"])
def test_sns_signature_and_topic_checks(monkeypatch, version):
    envelope = signed_notification({"contract": "inbound-email", "version": 1}, monkeypatch, version)
    assert email_provenance.has_valid_provenance(envelope, {})
    forged = dict(envelope, Message=json.dumps({"contract": "inbound-email", "sender": "fake"}))
    assert not email_provenance.has_valid_provenance(forged, {})
    assert not email_provenance.has_valid_provenance(dict(envelope, TopicArn="untrusted"), {})
    assert not email_provenance.has_valid_provenance(dict(envelope, SigningCertURL="https://evil.test/cert.pem"), {})
    unsigned = dict(envelope)
    unsigned.pop("Signature")
    assert not email_provenance.has_valid_provenance(unsigned, {})


def test_template_wires_trusted_publishers_and_zero_schedule():
    from test_template_alarms import load_alarms
    import yaml
    # Import registers the intrinsic constructors used by the template suite.
    template = yaml.load(open("template.yaml"), Loader=yaml.SafeLoader)["Resources"]
    variables = template["WorkerFunction"]["Properties"]["Environment"]["Variables"]
    assert variables["EMAIL_PUBLISHER_ROLE_IDS"] == {"Join": [",", [
        {"Fn::GetAtt": ["EmailIngressFunctionRole", "RoleId"]},
        {"Fn::GetAtt": ["ApiExecutionRole", "RoleId"]},
        {"Fn::GetAtt": ["WorkerFunctionRole", "RoleId"]}]]}
    assert variables["INBOUND_EMAIL_TOPICS"] == {"Ref": "InboundEmailTopicArn"}


def test_raw_sns_contract_requires_protected_queue_metadata(monkeypatch):
    queue = "arn:aws:sqs:eu-west-1:817685572750:dapier-events"
    monkeypatch.setenv("INBOUND_EMAIL_QUEUE_ARN", queue)
    raw = {"contract": "inbound-email", "version": 1, "event_id": "mail-1"}
    assert not email_provenance.has_valid_provenance(raw, {})
    assert not email_provenance.has_valid_provenance(dict(raw, eventSourceARN=queue), {})
    assert not email_provenance.has_valid_provenance(raw, {"eventSourceARN": queue + "-other"})
    assert email_provenance.has_valid_provenance(raw, {"eventSourceARN": queue})


def test_queue_policy_denies_arbitrary_direct_publishers():
    import yaml
    from test_template_alarms import load_alarms
    with open("template.yaml") as stream:
        resources = yaml.load(stream, Loader=yaml.SafeLoader)["Resources"]
    statements = resources["EventQueuePolicy"]["Properties"]["PolicyDocument"]["Statement"]
    deny = next(s for s in statements if s["Sid"] == "DenyUntrustedPublishers")
    assert deny["Effect"] == "Deny"
    assert deny["Principal"] == "*"
    assert deny["Action"] == "sqs:SendMessage"
    assert deny["Resource"] == {"Fn::GetAtt": ["EventQueue", "Arn"]}
    assert deny["Condition"] == {
        "BoolIfExists": {"aws:PrincipalIsAWSService": "false"},
        "ArnNotEquals": {"aws:PrincipalArn": [
            {"Fn::GetAtt": [role, "Arn"]} for role in ["ApiExecutionRole", "EmailIngressFunctionRole",
            "WorkerFunctionRole", "DropboxResolverFunctionRole", "HtmlRendererFunctionRole"]]}}
    sns = next(s for s in statements if s["Sid"] == "AllowInboundEmailEvents")
    assert sns["Principal"] == {"Service": "sns.amazonaws.com"}
    assert sns["Condition"]["ArnEquals"]["aws:SourceArn"] == {"Ref": "InboundEmailTopicArn"}
    variables = resources["WorkerFunction"]["Properties"]["Environment"]["Variables"]
    assert variables["INBOUND_EMAIL_QUEUE_ARN"] == {"Fn::GetAtt": ["EventQueue", "Arn"]}
    assert resources["WorkerFunction"]["DependsOn"] == "EventQueuePolicy"
