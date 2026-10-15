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
        for name, props in self.alarms.items():
            self.assertTrue(props["AlarmDescription"], name)
            self.assertIn(props["Namespace"], ("AWS/SQS", "AWS/Lambda", "Dapier/Triggers"), name)
            quiet = props["Namespace"] == "Dapier/Triggers"
            self.assertEqual("breaching" if quiet else "notBreaching",
                             props["TreatMissingData"], name)


class EmailQuietAlarmTests(unittest.TestCase):
    """Twelve hours without a single consumed email event pages the operator.

    An intake-side break (SES receipt rule, raw bucket notification, the
    intake Lambda, the event queue policy) leaves no backlog anywhere: the
    queue is simply empty, so the existing queue alarms stay green. The
    worker feeds this alarm one datapoint per email event it consumes.
    """

    def setUp(self):
        self.props = load_alarms()["EmailEventsQuietAlarm"]

    def test_pages_after_twelve_quiet_hours(self):
        self.assertEqual("Dapier/Triggers", self.props["Namespace"])
        self.assertEqual("EmailEventsReceived", self.props["MetricName"])
        self.assertEqual("Sum", self.props["Statistic"])
        self.assertEqual(3600, self.props["Period"])
        self.assertEqual(12, self.props["EvaluationPeriods"])
        self.assertEqual(1, self.props["Threshold"])
        self.assertEqual("LessThanThreshold", self.props["ComparisonOperator"])
        self.assertEqual("breaching", self.props["TreatMissingData"])

    def test_alarm_and_recovery_notify_the_alarm_topic(self):
        self.assertEqual([{"Ref": "AlarmTopic"}], self.props["AlarmActions"])
        self.assertEqual([{"Ref": "AlarmTopic"}], self.props["OKActions"])


if __name__ == "__main__":
    unittest.main()


def test_youtube_renewal_can_read_every_live_workflow_source():
    with open("template.yaml", encoding="utf-8") as fh:
        resource=yaml.load(fh, Loader=yaml.SafeLoader)["Resources"]["YouTubeSubscriptionFunction"]["Properties"]
    tables={policy["DynamoDBReadPolicy"]["TableName"]["Ref"] for policy in resource["Policies"] if "DynamoDBReadPolicy" in policy}
    assert tables == {"PublishedWorkflowsTable", "HookTriggersTable", "ScheduleTriggersTable", "PollTriggersTable"}
    assert resource["Events"]["Renewal"]["Properties"] == {"Schedule":"rate(4 days)","Enabled":True}
