"""Dense refusal metrics preserve Sum and allow natural alarm recovery."""
from unittest.mock import Mock

import boto3
import pytest
import yaml

from src.dapier.triggers import email_refusals
from test_template_alarms import load_alarms


def test_heartbeat_emits_zero_and_propagates_failure(monkeypatch):
    client = Mock()
    monkeypatch.setattr(boto3, "client", lambda name: client)
    email_refusals.heartbeat({}, None)
    client.put_metric_data.assert_called_once_with(Namespace="Dapier/Triggers", MetricData=[{
        "MetricName": "EmailSenderPolicyRefused", "Value": 0, "Unit": "Count"}])
    client.put_metric_data.side_effect = RuntimeError("unavailable")
    with pytest.raises(RuntimeError):
        email_refusals.heartbeat({}, None)



def test_template_schedules_zeros_without_changing_alarm():
    with open("template.yaml") as stream:
        template = yaml.load(stream, Loader=yaml.SafeLoader)["Resources"]
    heartbeat = template["EmailRefusalHeartbeatFunction"]["Properties"]
    assert heartbeat["Handler"] == "src.dapier.triggers.email_refusals.heartbeat"
    assert heartbeat["Events"]["Heartbeat"]["Properties"] == {
        "Schedule": "rate(1 minute)", "Enabled": True}
    assert heartbeat["Policies"] == [{"Statement": {
        "Effect": "Allow", "Action": "cloudwatch:PutMetricData", "Resource": "*",
        "Condition": {"StringEquals": {"cloudwatch:namespace": "Dapier/Triggers"}}}}]
    alarm = load_alarms()["EmailSenderPolicyRefusalAlarm"]
    assert {key: alarm[key] for key in ["MetricName", "Statistic", "Period", "EvaluationPeriods",
                                      "DatapointsToAlarm", "Threshold", "ComparisonOperator",
                                      "TreatMissingData", "AlarmActions"]} == {
        "MetricName": "EmailSenderPolicyRefused", "Statistic": "Sum", "Period": 3600,
        "EvaluationPeriods": 24, "DatapointsToAlarm": 1, "Threshold": 1,
        "ComparisonOperator": "GreaterThanOrEqualToThreshold", "TreatMissingData": "notBreaching",
        "AlarmActions": [{"Ref": "AlarmTopic"}]}
