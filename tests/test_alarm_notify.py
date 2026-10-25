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

    def test_event_queue_alarms_summarize_like_the_others(self):
        import json

        for alarm in ("EventQueueAgeAlarm", "EventQueueDepthAlarm"):
            message = json.dumps({
                "AlarmName": alarm,
                "NewStateReason": f"Threshold Crossed: 1 datapoint [{alarm}]",
            })
            body, subject = alarm_notify._summarize(sns_event(message), "op@example.com")
            self.assertEqual(subject, f"[dapier] alarm: {alarm}")
            self.assertIn(f"alarm: {alarm}", body)
            self.assertIn("Threshold Crossed", body)

    def test_several_records_collapse_into_one_subject(self):
        event = {"Records": [{"Sns": {"Message": "a"}}, {"Sns": {"Message": "b"}}]}
        body, subject = alarm_notify._summarize(event, "op@example.com")
        self.assertEqual(subject, "[dapier] 2 alarms fired")
        self.assertIn("alarm: unknown-alarm", body)


class HandlerTests(unittest.TestCase):
    def test_emails_the_operator_address_with_alert_sender(self):
        import json

        with patch.dict("os.environ", {
            "DAPIER_ALERT_SENDER": "DTC Alerts <alerts@dtcdev.click>",
            "DAPIER_EMAIL_SENDER": "no-reply@dtcdev.click",
            "DAPIER_NOTIFY_EMAIL": "ops@dtcdev.click",
        }, clear=False):
            with patch.object(alarm_notify, "_summarize",
                              return_value=("body", "[dapier] alarm: X")) as summarize:
                with patch("boto3.client") as client:
                    client.return_value.send_email.return_value = {"MessageId": "m1"}
                    result = alarm_notify.handler(sns_event(json.dumps({"AlarmName": "X"})))
        self.assertEqual(result, {"notified": "ops@dtcdev.click"})
        kwargs = client.return_value.send_email.call_args.kwargs
        self.assertEqual(kwargs["Source"], "DTC Alerts <alerts@dtcdev.click>")
        self.assertEqual(kwargs["Destination"]["ToAddresses"], ["ops@dtcdev.click"])
        client.assert_called_with("ses", region_name=unittest.mock.ANY)

    def test_emails_the_operator_address(self):
        import json

        with patch.dict("os.environ", {
            "DAPIER_EMAIL_SENDER": "no-reply@dtcdev.click",
            "DAPIER_NOTIFY_EMAIL": "ops@dtcdev.click",
        }, clear=False):
            with patch.object(alarm_notify, "_summarize",
                              return_value=("body", "[dapier] alarm: X")) as summarize:
                with patch("boto3.client") as client:
                    client.return_value.send_email.return_value = {"MessageId": "m1"}
                    result = alarm_notify.handler(sns_event(json.dumps({"AlarmName": "X"})))
        self.assertEqual(result, {"notified": "ops@dtcdev.click"})
        kwargs = client.return_value.send_email.call_args.kwargs
        self.assertEqual(kwargs["Source"], "no-reply@dtcdev.click")
        self.assertEqual(kwargs["Destination"]["ToAddresses"], ["ops@dtcdev.click"])
        client.assert_called_with("ses", region_name=unittest.mock.ANY)


if __name__ == "__main__":
    unittest.main()


def test_sender_refusal_alarm_includes_context_beyond_reason_cap():
    import json
    from src.dapier.triggers import email_refusals

    details = ("recipient: invoice@example.test\nsender: test@example.test\n"
               "rule: sender_not_in_allowlist\nworkflow: invoice-intake\n"
               "inbox/event id: event-1\nrun id: not started")
    message = {"AlarmName": "sender-refusal", "NewStateValue": "ALARM",
               "NewStateReason": "x" * 2500, "StateChangeTime": "2026-10-25T12:15:00Z",
               "Trigger": {"Namespace": "Dapier/Triggers", "MetricName": email_refusals.METRIC}}
    with patch.object(email_refusals, "alarm_details", return_value=details) as lookup:
        body, _ = alarm_notify._summarize(sns_event(json.dumps(message)), "ops@example.test")
    lookup.assert_called_once_with("2026-10-25T12:15:00Z")
    assert details in body
    assert "x" * 2001 not in body


def test_other_alarm_and_ok_transition_do_not_read_refusal_inbox():
    import json
    from src.dapier.triggers import email_refusals

    with patch.object(email_refusals, "alarm_details") as lookup:
        for namespace, metric, state in [("Other", email_refusals.METRIC, "ALARM"),
                                         ("Dapier/Triggers", "Other", "ALARM"),
                                         ("Dapier/Triggers", email_refusals.METRIC, "OK")]:
            alarm_notify._summarize(sns_event(json.dumps({
                "AlarmName": "X", "NewStateValue": state,
                "Trigger": {"Namespace": namespace, "MetricName": metric},
            })), "ops@example.test")
    lookup.assert_not_called()


def test_refusal_context_read_failure_retries_notification():
    import json
    import pytest
    from src.dapier.triggers import email_refusals

    message = {"AlarmName": "sender-refusal", "NewStateValue": "ALARM",
               "Trigger": {"Namespace": "Dapier/Triggers", "MetricName": email_refusals.METRIC}}
    with patch.object(email_refusals, "alarm_details", side_effect=RuntimeError("read unavailable")):
        with pytest.raises(RuntimeError, match="read unavailable"):
            alarm_notify.handler(sns_event(json.dumps(message)))
