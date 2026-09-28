"""Template checks for the CloudWatch alarms wired to the alarm topic.

Plain ``yaml.safe_load`` cannot read the template (it chokes on
CloudFormation shorthand tags), so unknown ``!Tag`` nodes become
``{"Tag": value}`` with the two intrinsics the alarms use normalized to
their long forms: ``!Ref X`` -> ``{"Ref": "X"}``,
``!GetAtt A.B`` -> ``{"Fn::GetAtt": ["A", "B"]}``.
"""

import unittest

import yaml


def _cfn_tag(loader, suffix, node):
    if isinstance(node, yaml.ScalarNode):
        value = loader.construct_scalar(node)
    elif isinstance(node, yaml.SequenceNode):
        value = loader.construct_sequence(node, deep=True)
    else:
        value = loader.construct_mapping(node, deep=True)
    if suffix == "GetAtt" and isinstance(value, str):
        logical, attr = value.split(".", 1)
        value = [logical, attr]
        suffix = "Fn::GetAtt"
    return {suffix: value}


yaml.add_multi_constructor("!", _cfn_tag, yaml.SafeLoader)


def load_alarms():
    with open("template.yaml", encoding="utf-8") as fh:
        resources = yaml.load(fh, Loader=yaml.SafeLoader)["Resources"]
    return {name: res["Properties"] for name, res in resources.items()
            if res["Type"] == "AWS::CloudWatch::Alarm"}


class EventQueueAlarmTests(unittest.TestCase):
    def setUp(self):
        self.alarms = load_alarms()

    def test_every_alarm_notifies_the_alarm_topic(self):
        for name, props in self.alarms.items():
            self.assertEqual([{"Ref": "AlarmTopic"}], props["AlarmActions"], name)

    def test_event_queue_age_alarm_watches_oldest_message(self):
        props = self.alarms["EventQueueAgeAlarm"]
        self.assertEqual("AWS/SQS", props["Namespace"])
        self.assertEqual("ApproximateAgeOfOldestMessage", props["MetricName"])
        self.assertEqual(
            [{"Name": "QueueName", "Value": {"Fn::GetAtt": ["EventQueue", "QueueName"]}}],
            props["Dimensions"])
        # 900s = full redrive cycle (5 receives x 180s visibility): past it
        # the message is wedged without erroring, so the DLQ alarm stays quiet.
        self.assertEqual(900, props["Threshold"])
        self.assertEqual("Maximum", props["Statistic"])
        self.assertEqual(60, props["Period"])
        self.assertEqual(1, props["EvaluationPeriods"])
        self.assertEqual("GreaterThanOrEqualToThreshold", props["ComparisonOperator"])
        self.assertEqual("notBreaching", props["TreatMissingData"])

    def test_event_queue_depth_alarm_watches_backlog(self):
        props = self.alarms["EventQueueDepthAlarm"]
        self.assertEqual("AWS/SQS", props["Namespace"])
        self.assertEqual("ApproximateNumberOfMessagesVisible", props["MetricName"])
        self.assertEqual(
            [{"Name": "QueueName", "Value": {"Fn::GetAtt": ["EventQueue", "QueueName"]}}],
            props["Dimensions"])
        self.assertEqual(100, props["Threshold"])
        self.assertEqual("Maximum", props["Statistic"])
        self.assertEqual(300, props["Period"])
        self.assertEqual(1, props["EvaluationPeriods"])
        self.assertEqual("GreaterThanOrEqualToThreshold", props["ComparisonOperator"])
        self.assertEqual("notBreaching", props["TreatMissingData"])

    def test_queue_alarms_match_existing_alarm_shape(self):
        for props in self.alarms.values():
            self.assertTrue(props["AlarmDescription"])
            self.assertIn(props["Namespace"], ("AWS/SQS", "AWS/Lambda"))
            self.assertEqual("notBreaching", props["TreatMissingData"])


if __name__ == "__main__":
    unittest.main()
