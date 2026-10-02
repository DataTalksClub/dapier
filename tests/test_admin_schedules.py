"""The console's schedule surface: /api/admin/schedule-triggers.

The Schedules view (src/web/js/views/schedules.js) drives exactly these
three routes — list, save, delete — over the same api.schedule_triggers
domain `dapier schedules` reaches through /api/agent/schedule-triggers.
Save reprograms the EventBridge rule before the record is stored, and the
view's "Turn off"/"Turn on" is a full restatement with enabled flipped
(the PUT body is the whole item), so the roundtrip below mirrors that.
"""

import json
import time

from src.dapier.api import admin
from src.dapier.auth import session


class ScheduleTable:
    """In-memory SCHEDULE_TRIGGERS_TABLE (boto3 resource API surface)."""

    def __init__(self):
        self.items = {}

    def get_item(self, **kwargs):
        item = self.items.get(kwargs["Key"]["schedule_id"])
        return {"Item": dict(item)} if item else {}

    def put_item(self, **kwargs):
        self.items[kwargs["Item"]["schedule_id"]] = dict(kwargs["Item"])

    def delete_item(self, **kwargs):
        self.items.pop(kwargs["Key"]["schedule_id"], None)

    def scan(self, **kwargs):
        return {"Items": [dict(item) for item in self.items.values()]}


class FakeEvents:
    """Just enough of the EventBridge client: record rule programming."""

    def __init__(self):
        self.rules = {}

    def put_rule(self, **kwargs):
        self.rules[kwargs["Name"]] = dict(kwargs)
        return {"RuleArn": f"arn:aws:events:eu-west-1::rule/{kwargs['Name']}"}

    def put_targets(self, **kwargs):
        self.rules[kwargs["Rule"]]["targets"] = kwargs["Targets"]

    def remove_targets(self, **kwargs):
        pass

    def delete_rule(self, **kwargs):
        self.rules.pop(kwargs["Name"], None)


def configure_console(monkeypatch):
    table = ScheduleTable()
    events = FakeEvents()

    class Dynamo:
        def Table(self, name):
            return table

    import boto3

    monkeypatch.setenv("SCHEDULE_TRIGGERS_TABLE", "schedules")
    monkeypatch.setenv("WORKER_FUNCTION_ARN", "arn:aws:lambda:eu-west-1::function:worker")
    monkeypatch.delenv("AUDIT_TABLE", raising=False)
    monkeypatch.setattr(boto3, "resource", lambda service: Dynamo())
    monkeypatch.setattr(boto3, "client", lambda service: events)
    monkeypatch.setattr(session, "_credentials",
                        lambda: {"username": "admin", "password": "pw"})
    cookie = session._sign({"sub": "op@datatalks.club", "subject": "op-sub",
                            "exp": int(time.time()) + 600})
    return {"cookies": [f"dapier_session={cookie}"], "table": table, "events": events}


def admin_request(method, path, body=None, cookies=None, query=None):
    event = {
        "requestContext": {"http": {"method": method, "path": path}},
        "headers": {"host": "dapier.example.test",
                    "origin": "https://dapier.example.test"},
        "cookies": cookies or [],
    }
    if query is not None:
        event["queryStringParameters"] = query
    if body is not None:
        event["body"] = json.dumps(body)
    return event


def test_admin_schedule_list_requires_signin():
    response = admin.route(
        admin_request("GET", "/api/admin/schedule-triggers"),
        "GET", "/api/admin/schedule-triggers")
    assert response["statusCode"] == 401


def test_admin_schedule_create_toggle_delete(monkeypatch):
    console = configure_console(monkeypatch)
    cookies, events, table = console["cookies"], console["events"], console["table"]

    # Create: the view's "New schedule" dialog posts the whole item.
    saved = admin.route(
        admin_request("PUT", "/api/admin/schedule-triggers",
                      body={"name": "morning-digest", "expression": "cron(0 8 * * ? *)",
                            "actions": [{"type": "webhook", "url": "https://hooks.test/x"}],
                            "description": "Daily digest"},
                      cookies=cookies),
        "PUT", "/api/admin/schedule-triggers")
    assert saved["statusCode"] == 200, saved
    body = json.loads(saved["body"])
    assert body["created"] is True
    assert body["schedule_id"] == "morning-digest"
    assert body["enabled"] is True
    assert body["rule"] == "dapier-schedule-morning-digest"
    assert events.rules["dapier-schedule-morning-digest"]["State"] == "ENABLED"

    # List: the view's table rows.
    listed = admin.route(
        admin_request("GET", "/api/admin/schedule-triggers", cookies=cookies),
        "GET", "/api/admin/schedule-triggers")
    assert listed["statusCode"] == 200
    payload = json.loads(listed["body"])
    assert [item["schedule_id"] for item in payload["schedules"]] == ["morning-digest"]
    row = payload["schedules"][0]
    assert row["expression"] == "cron(0 8 * * ? *)"
    assert row["description"] == "Daily digest"
    assert row["enabled"] is True

    # "Turn off" restates the whole item with enabled flipped; the rule is
    # reprogrammed to DISABLED and the record is updated, not recreated.
    toggled = admin.route(
        admin_request("PUT", "/api/admin/schedule-triggers",
                      body={"name": "morning-digest", "expression": "cron(0 8 * * ? *)",
                            "actions": [{"type": "webhook", "url": "https://hooks.test/x"}],
                            "enabled": False},
                      cookies=cookies),
        "PUT", "/api/admin/schedule-triggers")
    assert toggled["statusCode"] == 200
    body = json.loads(toggled["body"])
    assert body["created"] is False
    assert body["enabled"] is False
    assert events.rules["dapier-schedule-morning-digest"]["State"] == "DISABLED"

    # Delete: confirm-dialog path — the rule is torn down and the item removed.
    deleted = admin.route(
        admin_request("DELETE", "/api/admin/schedule-triggers",
                      query={"name": "morning-digest"}, cookies=cookies),
        "DELETE", "/api/admin/schedule-triggers")
    assert deleted["statusCode"] == 200
    assert json.loads(deleted["body"]) == {"ok": True, "schedule_id": "morning-digest"}
    assert "dapier-schedule-morning-digest" not in events.rules
    assert table.items == {}


def test_admin_schedule_delete_unknown_is_404(monkeypatch):
    console = configure_console(monkeypatch)
    response = admin.route(
        admin_request("DELETE", "/api/admin/schedule-triggers",
                      query={"name": "missing"}, cookies=console["cookies"]),
        "DELETE", "/api/admin/schedule-triggers")
    assert response["statusCode"] == 404
    assert "missing" in json.loads(response["body"])["error"]


def test_admin_schedule_save_invalid_body_is_400(monkeypatch):
    console = configure_console(monkeypatch)
    event = admin_request("PUT", "/api/admin/schedule-triggers",
                          cookies=console["cookies"])
    event["body"] = "not json"
    response = admin.route(event, "PUT", "/api/admin/schedule-triggers")
    assert response["statusCode"] == 400


def test_admin_schedule_save_rejects_bad_expression(monkeypatch):
    console = configure_console(monkeypatch)
    response = admin.route(
        admin_request("PUT", "/api/admin/schedule-triggers",
                      body={"name": "broken", "expression": "every tuesday",
                            "actions": [{"type": "webhook", "url": "https://hooks.test/x"}]},
                      cookies=console["cookies"]),
        "PUT", "/api/admin/schedule-triggers")
    assert response["statusCode"] == 400, response
    assert "expression" in json.loads(response["body"])["error"]
    assert console["table"].items == {}
