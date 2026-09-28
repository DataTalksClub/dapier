"""Tests for the CloudWatch alarm email notifier (src/dapier/alarm_notify.py)."""

import unittest
from unittest.mock import patch

from src.dapier import alarm_notify


def sns_event(message):
    return {"Records": [{"Sns": {"Message": message}}]}


class SummarizeTests(unittest.TestCase):
    def test_parses_cloudwatch_notification(self):
        import json

        message = json.dumps({
            "AlarmName": "EventDeadLetterAlarm",
            "NewStateReason": "Threshold Crossed: 1 datapoint [1.0]",
        })
        body, subject = alarm_notify._summarize(sns_event(message), "op@example.com")
        self.assertEqual(subject, "[dapier] alarm: EventDeadLetterAlarm")
        self.assertIn("alarm: EventDeadLetterAlarm", body)
        self.assertIn("Threshold Crossed", body)

    def test_non_json_message_is_quoted_verbatim(self):
        body, subject = alarm_notify._summarize(sns_event("plain garbage"), "op@example.com")
        self.assertEqual(subject, "[dapier] alarm: unknown-alarm")
        self.assertIn("plain garbage", body)

    def test_several_records_collapse_into_one_subject(self):
        event = {"Records": [{"Sns": {"Message": "a"}}, {"Sns": {"Message": "b"}}]}
        body, subject = alarm_notify._summarize(event, "op@example.com")
        self.assertEqual(subject, "[dapier] 2 alarms fired")
        self.assertIn("alarm: unknown-alarm", body)


class HandlerTests(unittest.TestCase):
    def test_emails_the_operator_address(self):
        import json

        with patch.dict("os.environ", {"DAPIER_EMAIL_SENDER": "ops@dtcdev.click"}):
            with patch.object(alarm_notify, "_summarize",
                              return_value=("body", "[dapier] alarm: X")) as summarize:
                with patch("boto3.client") as client:
                    client.return_value.send_email.return_value = {"MessageId": "m1"}
                    result = alarm_notify.handler(sns_event(json.dumps({"AlarmName": "X"})))
        self.assertEqual(result, {"notified": "ops@dtcdev.click"})
        kwargs = client.return_value.send_email.call_args.kwargs
        self.assertEqual(kwargs["Source"], "ops@dtcdev.click")
        self.assertEqual(kwargs["Destination"]["ToAddresses"], ["ops@dtcdev.click"])


if __name__ == "__main__":
    unittest.main()
