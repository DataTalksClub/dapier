"""Tests for cron/rate schedule triggers (src/schedule_triggers.py)."""

import json
import os
import unittest
from unittest.mock import patch

from src import agent_api, schedule_triggers, worker
from src.engine import all_workflows, matches


class StubTable:
    def __init__(self, items=None):
        self.items = {item["schedule_id"]: dict(item) for item in (items or [])}

    def scan(self, Limit=200):
        return {"Items": [dict(value) for value in self.items.values()]}

    def get_item(self, Key):
        item = self.items.get(Key["schedule_id"])
        return {"Item": dict(item)} if item else {}

    def put_item(self, Item):
        self.items[Item["schedule_id"]] = dict(Item)

    def delete_item(self, Key):
        self.items.pop(Key["schedule_id"], None)


class StubEvents:
    def __init__(self, fail_on=None):
        self.calls = []
        self.fail_on = fail_on or set()

    def _record(self, method, **kwargs):
        self.calls.append((method, kwargs))
        if method in self.fail_on:
            from botocore.exceptions import ClientError

            raise ClientError({"Error": {"Code": "ValidationException"}}, method)

    def put_rule(self, **kwargs):
        self._record("put_rule", **kwargs)
        return {"RuleArn": f"arn:aws:events:eu-west-1:1:rule/{kwargs['Name']}"}

    def put_targets(self, **kwargs):
        self._record("put_targets", **kwargs)
        return {}

    def remove_targets(self, **kwargs):
        self._record("remove_targets", **kwargs)
        return {}

    def delete_rule(self, **kwargs):
        self._record("delete_rule", **kwargs)
        return {}


WORKER_ARN = "arn:aws:lambda:eu-west-1:1:function:dapier-worker"
SCHEDULE_BODY = {
    "name": "morning-digest",
    "expression": "cron(0 8 * * ? *)",
    "actions": [{"type": "webhook", "url": "https://hooks.test/x"}],
}
ENV = {"SCHEDULE_TRIGGERS_TABLE": "schedules", "WORKER_FUNCTION_ARN": WORKER_ARN}


class ExpressionTests(unittest.TestCase):
    def test_accepts_cron_and_rate(self):
        for expression in ("cron(0 8 * * ? *)", "rate(5 minutes)", "rate(1 day)"):
            schedule_triggers.validate_expression(expression)

    def test_rejects_bad_expressions(self):
        for expression in ("cron(* * * *)", "rate(sometimes)", "daily", ""):
            with self.assertRaises(schedule_triggers.TriggerError):
                schedule_triggers.validate_expression(expression)


class SaveTests(unittest.TestCase):
    def test_save_programs_rule_and_target(self):
        events = StubEvents()
        with patch.dict(os.environ, ENV):
            status, payload = schedule_triggers.api_save(
                dict(SCHEDULE_BODY), "op", table_ref=StubTable(), events_client=events)
        self.assertEqual(status, 200)
        self.assertTrue(payload["created"])
        self.assertEqual(payload["rule"], "dapier-schedule-morning-digest")
        methods = [method for method, _ in events.calls]
        self.assertEqual(methods, ["put_rule", "put_targets"])
        _, rule_kwargs = events.calls[0]
        self.assertEqual(rule_kwargs["ScheduleExpression"], "cron(0 8 * * ? *)")
        self.assertEqual(rule_kwargs["State"], "ENABLED")
        _, target_kwargs = events.calls[1]
        target = target_kwargs["Targets"][0]
        self.assertEqual(target["Arn"], WORKER_ARN)
        self.assertEqual(
            json.loads(target["Input"]),
            {"trigger": "schedule", "schedule_id": "morning-digest"})

    def test_save_disabled_disables_the_rule(self):
        events = StubEvents()
        body = dict(SCHEDULE_BODY, enabled=False)
        with patch.dict(os.environ, ENV):
            schedule_triggers.api_save(
                body, "op", table_ref=StubTable(), events_client=events)
        _, rule_kwargs = events.calls[0]
        self.assertEqual(rule_kwargs["State"], "DISABLED")

    def test_failed_rule_sync_stores_nothing(self):
        events = StubEvents(fail_on={"put_rule"})
        table = StubTable()
        with patch.dict(os.environ, ENV):
            with self.assertRaises(Exception):
                schedule_triggers.api_save(
                    dict(SCHEDULE_BODY), "op", table_ref=table, events_client=events)
        self.assertEqual(table.items, {})

    def test_delete_removes_rule_and_item(self):
        events = StubEvents()
        table = StubTable()
        with patch.dict(os.environ, ENV):
            schedule_triggers.api_save(
                dict(SCHEDULE_BODY), "op", table_ref=table, events_client=events)
            status, payload = schedule_triggers.api_delete(
                "morning-digest", "op", table_ref=table, events_client=events)
        self.assertEqual(status, 200)
        self.assertEqual(table.items, {})
        self.assertEqual(
            [method for method, _ in events.calls][-2:],
            ["remove_targets", "delete_rule"])


class EngineTests(unittest.TestCase):
    def test_schedule_workflow_matches_event(self):
        table = StubTable()
        with patch.dict(os.environ, ENV):
            schedule_triggers.api_save(
                dict(SCHEDULE_BODY), "op", table_ref=table,
                events_client=StubEvents(), target_arn=WORKER_ARN)
            with patch.object(schedule_triggers, "get_table", return_value=table):
                workflows = all_workflows()
        schedule_workflows = [wf for wf in workflows if wf["id"].startswith("schedule-trigger-")]
        self.assertEqual(len(schedule_workflows), 1)
        event = {"connector": "schedule", "event": "schedule.triggered",
                 "data": {"schedule": "morning-digest"}}
        self.assertTrue(matches(schedule_workflows[0], event))
        self.assertFalse(matches(schedule_workflows[0], {"connector": "schedule",
                                                        "event": "schedule.triggered",
                                                        "data": {"schedule": "other"}}))


class WorkerTests(unittest.TestCase):
    def test_handler_runs_the_fired_schedule(self):
        seen = {}

        def fake_execute(event, **_kwargs):
            seen["event"] = event

        fire = {"trigger": "schedule", "schedule_id": "morning-digest"}
        with patch.object(worker, "execute", side_effect=fake_execute):
            result = worker.handler(dict(fire), None)
        self.assertEqual(result, {"executed": "morning-digest"})
        event = seen["event"]
        self.assertEqual(event["connector"], "schedule")
        self.assertEqual(event["event"], "schedule.triggered")
        self.assertEqual(event["data"]["schedule"], "morning-digest")
        self.assertTrue(event["id"].startswith("morning-digest-"))
        self.assertTrue(event["occurred_at"])

    def test_non_schedule_events_keep_sqs_path(self):
        with patch.object(worker, "execute") as execute:
            result = worker.handler({"Records": []}, None)
        self.assertEqual(result, {"batchItemFailures": []})
        execute.assert_not_called()


class AgentApiTests(unittest.TestCase):
    def test_operator_can_create_and_list_schedules(self):
        stub = StubTable()
        event = {"headers": {}, "body": json.dumps(dict(SCHEDULE_BODY))}
        with patch("src.agent_api.require_operator", return_value=("sub-1", None)), \
             patch("src.schedule_triggers.sync_rule") as sync_rule, \
             patch("src.schedule_triggers.get_table", return_value=stub), \
             patch.dict(os.environ, ENV):
            created = agent_api.schedule_triggers_api(event, "PUT")
            listed = agent_api.schedule_triggers_api({"headers": {}}, "GET")
        self.assertEqual(created["statusCode"], 200)
        self.assertTrue(json.loads(created["body"])["created"])
        self.assertEqual(
            [s["schedule_id"] for s in json.loads(listed["body"])["schedules"]],
            ["morning-digest"])
        sync_rule.assert_called_once()

    def test_non_operator_is_rejected(self):
        with patch("src.agent_api.require_operator",
                   return_value=(None, {"statusCode": 403})):
            response = agent_api.schedule_triggers_api({"headers": {}}, "GET")
        self.assertEqual(response["statusCode"], 403)

    def test_bad_expression_is_400(self):
        stub = StubTable()
        body = dict(SCHEDULE_BODY, expression="whenever")
        with patch("src.agent_api.require_operator", return_value=("sub-1", None)), \
             patch("src.schedule_triggers.get_table", return_value=stub), \
             patch.dict(os.environ, ENV):
            response = agent_api.schedule_triggers_api(
                {"headers": {}, "body": json.dumps(body)}, "PUT")
        self.assertEqual(response["statusCode"], 400)


if __name__ == "__main__":
    unittest.main()
