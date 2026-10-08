"""The Schedules tab's activity + health: "when do things run, and did they?"

Covers the pieces the console view and `dapier schedules` share:
plain-language cron (schedule_times.describe), next fires, the fire history
the worker notes on each schedule, the health verdict, Upcoming, Run now,
and Pause/Resume — through the admin routes, the agent routes, and the CLI.
"""

import json
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest

from dapier_cli import commands, main
from src.dapier.api import admin
from src.dapier.api import agent as agent_api
from src.dapier.engine import worker
from src.dapier.triggers import schedule_times, schedule_triggers

from tests.test_admin_schedules import admin_request, configure_console

NOW = datetime(2026, 10, 8, 15, 30, tzinfo=timezone.utc)  # a Thursday


def iso(moment):
    return moment.isoformat()


class Table:
    def __init__(self, items=()):
        self.items = {item["schedule_id"]: dict(item) for item in items}

    def scan(self, **kwargs):
        return {"Items": [dict(item) for item in self.items.values()]}

    def get_item(self, Key):
        item = self.items.get(Key["schedule_id"])
        return {"Item": dict(item)} if item else {}

    def put_item(self, Item, **kwargs):
        self.items[Item["schedule_id"]] = dict(Item)

    def delete_item(self, Key):
        self.items.pop(Key["schedule_id"], None)


def listening(name, workflow_id="digest-flow", enabled=True):
    return {"id": workflow_id, "enabled": enabled, "actions": [],
            "trigger": {"connector": "schedule", "event": "schedule.triggered",
                        "filters": {"schedule": {"equals": name}}}}


# --- plain language and fire times -----------------------------------------------

@pytest.mark.parametrize("expression, text", [
    ("cron(0 9 ? * MON-FRI *)", "every weekday at 09:00 UTC"),
    ("cron(0 7 * * ? *)", "every day at 07:00 UTC"),
    ("cron(0/15 * * * ? *)", "every 15 minutes"),
    ("cron(0 * * * ? *)", "every hour at :00"),
    ("cron(30 8 1 * ? *)", "on the 1st of the month at 08:30 UTC"),
    ("cron(0 9 ? * 2#1 *)", "on the 1st Monday of the month at 09:00 UTC"),
    ("cron(0 9 L * ? *)", "on the last day of the month at 09:00 UTC"),
    ("cron(0 9,17 ? * * *)", "every day at 09:00 and 17:00 UTC"),
    ("cron(0 9 ? * MON * Europe/Berlin)", "every Monday at 09:00 Europe/Berlin"),
    ("cron(0 9-17 ? * MON-FRI *)", "every hour from 09:00 to 17:00 UTC on weekdays"),
    ("rate(1 hour)", "every hour"),
    ("rate(15 minutes)", "every 15 minutes"),
    ("rate(2 days)", "every 2 days"),
    ("cron(5,10 * * * ? *)", "on a custom cron schedule"),
])
def test_describe_reads_expressions_plainly(expression, text):
    assert schedule_times.describe(expression) == text


def test_next_fires_cron_skips_the_weekend():
    times = schedule_times.next_fires("cron(0 9 ? * MON-FRI *)", NOW, count=3)
    assert [t.strftime("%a %H:%M") for t in times] == ["Fri 09:00", "Mon 09:00", "Tue 09:00"]


def test_next_fires_honours_the_cron_timezone():
    # 09:00 Berlin is 07:00 UTC in summer time, 08:00 UTC after the switch.
    times = schedule_times.next_fires("cron(0 9 ? * MON * Europe/Berlin)", NOW, count=3)
    assert [t.strftime("%d %H:%M") for t in times] == ["12 07:00", "19 07:00", "26 08:00"]


def test_next_fires_rate_counts_from_its_anchor():
    times = schedule_times.next_fires("rate(1 hour)", NOW, count=2,
                                      anchor=NOW - timedelta(minutes=150))
    assert [t.strftime("%H:%M") for t in times] == ["16:00", "17:00"]


def test_unreadable_expression_has_no_fires():
    assert schedule_times.next_fires("cron(nope)", NOW) == []
    assert schedule_times.typical_interval("cron(0 9 1 1 ? 2030)", NOW) is None


def test_weekday_interval_reads_daily():
    assert schedule_times.typical_interval("cron(0 9 ? * MON-FRI *)", NOW) == 86400


# --- history and health -------------------------------------------------------------

def test_list_view_carries_next_runs_history_health_and_listeners():
    table = Table([{
        "schedule_id": "hourly", "expression": "cron(0 * * * ? *)", "enabled": True,
        "updated_at": iso(NOW - timedelta(days=5)),
        "fires": [{"at": iso(NOW - timedelta(days=3)), "outcome": "ran",
                   "event_id": "hourly-1", "workflows": ["digest-flow"]}],
    }])
    status, payload = schedule_triggers.api_list(
        table, now=NOW, workflows=[listening("hourly")])
    assert status == 200
    row = payload["schedules"][0]
    assert row["summary"] == "every hour at :00"
    assert row["next_runs"][0] == "2026-10-08T16:00:00Z"
    assert row["workflows"] == [{"id": "digest-flow", "name": "digest-flow", "enabled": True}]
    assert row["fires"][0]["runs"] == [{"workflow_id": "digest-flow",
                                        "run_id": "digest-flow:hourly-1"}]
    assert row["last_outcome"] == "ran"
    # Silent for three days on an hourly schedule: it stands out.
    assert row["health"] == {"state": "attention",
                             "reason": "Last fired 3 days ago; expected every hour."}


def test_health_states():
    on_time = {"schedule_id": "s", "expression": "cron(0 * * * ? *)", "enabled": True,
               "updated_at": iso(NOW - timedelta(days=1)),
               "fires": [{"at": iso(NOW - timedelta(minutes=30)), "outcome": "ran"}]}
    reached = [{"id": "w", "enabled": True}]
    assert schedule_triggers.health(on_time, NOW, reached)["state"] == "ok"
    assert schedule_triggers.health({**on_time, "enabled": False}, NOW, reached)["state"] == "paused"
    fresh = {**on_time, "fires": [], "updated_at": iso(NOW - timedelta(minutes=5))}
    assert schedule_triggers.health(fresh, NOW, reached)["state"] == "waiting"
    failed = {**on_time, "fires": [{"at": iso(NOW - timedelta(minutes=30)),
                                    "outcome": "failed", "error": "boom"}]}
    assert schedule_triggers.health(failed, NOW, reached) == {
        "state": "attention", "reason": "The last fire failed: boom"}
    nobody = schedule_triggers.health(on_time, NOW, [])
    assert nobody["state"] == "attention" and "No published workflow" in nobody["reason"]
    # A manual Run now does not hide a stopped schedule.
    stopped = {**on_time, "fires": [
        {"at": iso(NOW - timedelta(minutes=1)), "outcome": "ran", "manual": True},
        {"at": iso(NOW - timedelta(days=2)), "outcome": "ran"}]}
    assert schedule_triggers.health(stopped, NOW, reached)["state"] == "attention"


def test_record_fire_keeps_newest_first_capped_and_replaces_a_retry():
    table = Table([{"schedule_id": "s", "expression": "rate(1 hour)", "enabled": True}])
    for index in range(schedule_triggers.FIRES_KEEP + 3):
        event = schedule_triggers.fire_event("s", now=NOW + timedelta(hours=index))
        schedule_triggers.record_fire(event, "ran", ["w"], table_ref=table)
    fires = table.items["s"]["fires"]
    assert len(fires) == schedule_triggers.FIRES_KEEP
    assert fires[0]["at"] > fires[-1]["at"]
    retry = schedule_triggers.fire_event("s", now=NOW + timedelta(days=2))
    schedule_triggers.record_fire(retry, "retrying", ["w"], "boom", table_ref=table)
    schedule_triggers.record_fire(retry, "ran", ["w"], table_ref=table)
    fires = table.items["s"]["fires"]
    assert [fire["outcome"] for fire in fires[:2]] == ["ran", "ran"]
    assert sum(1 for fire in fires if fire["event_id"] == retry["id"]) == 1


def test_record_fire_never_recreates_a_deleted_schedule():
    table = Table()
    event = schedule_triggers.fire_event("gone", now=NOW)
    assert schedule_triggers.record_fire(event, "ran", [], table_ref=table) is None
    assert table.items == {}


def test_save_keeps_history_and_notes_pause_and_resume():
    table = Table([{"schedule_id": "sched", "expression": "rate(1 hour)", "enabled": True,
                    "fires": [{"at": iso(NOW), "outcome": "ran"}]}])
    with patch.object(schedule_triggers, "sync_rule"):
        schedule_triggers.api_set_enabled("sched", False, "op", table_ref=table)
        assert [f["outcome"] for f in table.items["sched"]["fires"]] == ["paused", "ran"]
        assert table.items["sched"]["enabled"] is False
        schedule_triggers.api_save({"name": "sched", "expression": "rate(1 hour)",
                                    "enabled": True}, "op", table_ref=table)
    assert [f["outcome"] for f in table.items["sched"]["fires"]] == ["resumed", "paused", "ran"]
    assert table.items["sched"]["fires"][0]["by"] == "op"


def test_upcoming_lists_fires_and_summarizes_frequent_schedules():
    table = Table([
        {"schedule_id": "digest", "expression": "cron(0 9 ? * MON-FRI *)", "enabled": True},
        {"schedule_id": "tick", "expression": "rate(5 minutes)", "enabled": True,
         "updated_at": iso(NOW)},
        {"schedule_id": "off", "expression": "cron(0 10 * * ? *)", "enabled": False},
    ])
    status, payload = schedule_triggers.api_upcoming(
        24 * 7, table, now=NOW, workflows=[listening("digest")])
    assert status == 200
    assert [entry["at"][:16] for entry in payload["upcoming"]] == [
        "2026-10-09T09:00", "2026-10-12T09:00", "2026-10-13T09:00",
        "2026-10-14T09:00", "2026-10-15T09:00"]
    assert payload["upcoming"][0]["workflows"] == ["digest-flow"]
    assert payload["frequent"] == [{
        "schedule_id": "tick", "summary": "every 5 minutes", "count": 2016,
        "first_at": "2026-10-08T15:35:00Z", "approximate": True, "workflows": []}]
    assert schedule_triggers.api_upcoming(0, table)[0] == 400
    assert schedule_triggers.api_upcoming(999, table)[0] == 400


def test_run_now_queues_a_manual_fire():
    table = Table([{"schedule_id": "digest", "expression": "rate(1 day)", "enabled": False}])
    sent = []

    class Queue:
        def send_message(self, **kwargs):
            sent.append(json.loads(kwargs["MessageBody"]))

    with patch.dict("os.environ", {"EVENT_QUEUE_URL": "https://sqs.test/q"}), \
         patch.object(schedule_triggers, "load_workflows", return_value=[listening("digest")]):
        status, payload = schedule_triggers.api_run_now("digest", "op", table_ref=table,
                                                        queue=Queue())
    assert status == 202
    event = sent[0]
    assert event["connector"] == "schedule" and event["data"]["schedule"] == "digest"
    assert event["data"]["manual"] is True and event["data"]["requested_by"] == "op"
    assert payload["runs"] == [{"workflow_id": "digest-flow",
                                "run_id": f"digest-flow:{event['id']}"}]
    with pytest.raises(schedule_triggers.TriggerError):
        schedule_triggers.api_run_now("missing", "op", table_ref=table, queue=Queue())


# --- the worker notes every fire ----------------------------------------------------

def test_worker_records_a_scheduled_fire():
    recorded = []
    with patch.object(worker, "execute", return_value=["digest-flow"]), \
         patch.object(schedule_triggers, "record_fire",
                      side_effect=lambda *args, **kw: recorded.append(args)):
        worker.handler({"trigger": "schedule", "schedule_id": "digest"}, None)
    event, outcome, matched, error = recorded[0]
    assert event["data"]["schedule"] == "digest"
    assert (outcome, matched, error) == ("ran", ["digest-flow"], None)


def test_worker_records_a_fire_nothing_listens_to():
    recorded = []
    with patch.object(worker, "execute", return_value=[]), \
         patch.object(schedule_triggers, "record_fire",
                      side_effect=lambda *args, **kw: recorded.append(args)):
        worker.handler({"trigger": "schedule", "schedule_id": "digest"}, None)
    assert recorded[0][1] == "no_listeners"


def test_worker_records_a_failed_fire():
    recorded = []
    with patch.object(worker, "execute", side_effect=RuntimeError("boom")), \
         patch.object(worker, "_schedule_retry", return_value=False), \
         patch.object(worker, "_emit_failure_notice"), \
         patch.object(worker, "_auto_pause_on_failure", return_value=False), \
         patch.object(schedule_triggers, "record_fire",
                      side_effect=lambda *args, **kw: recorded.append(args)):
        with pytest.raises(RuntimeError):
            worker.handler({"trigger": "schedule", "schedule_id": "digest"}, None)
    assert recorded[0][1] == "failed" and str(recorded[0][3]) == "boom"


def test_worker_records_a_queued_run_now():
    recorded = []
    event = schedule_triggers.fire_event("digest", manual=True, requested_by="op", now=NOW)
    record = {"messageId": "m1", "body": json.dumps(event), "attributes": {}}
    with patch.object(worker, "execute", return_value=["digest-flow"]), \
         patch.object(worker.inbox, "record", return_value=None), \
         patch.object(schedule_triggers, "record_fire",
                      side_effect=lambda *args, **kw: recorded.append(args)):
        result = worker.handler({"Records": [record]}, None)
    assert result == {"batchItemFailures": []}
    assert recorded[0][0]["data"]["manual"] is True
    assert recorded[0][1:3] == ("ran", ["digest-flow"])


def test_fire_entry_marks_manual_runs():
    event = schedule_triggers.fire_event("digest", manual=True, requested_by="op", now=NOW)
    entry = schedule_triggers.fire_entry(event, "ran", ["w"])
    assert entry["manual"] is True and entry["by"] == "op"


# --- console routes (/api/admin) ---------------------------------------------------

def test_admin_pause_resume_run_and_upcoming(monkeypatch):
    console = configure_console(monkeypatch)
    cookies, events, table = console["cookies"], console["events"], console["table"]
    admin.route(admin_request("PUT", "/api/admin/schedule-triggers",
                              body={"name": "digest", "expression": "cron(0 9 ? * MON-FRI *)"},
                              cookies=cookies),
                "PUT", "/api/admin/schedule-triggers")

    def post(path):
        return admin.route(admin_request("POST", path, body={}, cookies=cookies), "POST", path)

    paused = post("/api/admin/schedule-triggers/digest/pause")
    assert paused["statusCode"] == 200, paused
    assert json.loads(paused["body"])["enabled"] is False
    assert events.rules["dapier-schedule-digest"]["State"] == "DISABLED"
    resumed = post("/api/admin/schedule-triggers/digest/resume")
    assert json.loads(resumed["body"])["changed"] is True
    assert events.rules["dapier-schedule-digest"]["State"] == "ENABLED"
    assert [f["outcome"] for f in table.items["digest"]["fires"]] == ["resumed", "paused"]
    assert post("/api/admin/schedule-triggers/missing/pause")["statusCode"] == 404

    sent = []
    events.send_message = lambda **kwargs: sent.append(kwargs)
    monkeypatch.setenv("EVENT_QUEUE_URL", "https://sqs.test/q")
    ran = post("/api/admin/schedule-triggers/digest/run")
    assert ran["statusCode"] == 202
    assert json.loads(sent[0]["MessageBody"])["data"]["manual"] is True

    path = "/api/admin/schedule-triggers/upcoming"
    upcoming = admin.route(admin_request("GET", path, cookies=cookies,
                                         query={"hours": "168"}), "GET", path)
    assert upcoming["statusCode"] == 200
    body = json.loads(upcoming["body"])
    assert body["hours"] == 168 and len(body["upcoming"]) == 5

    listed = admin.route(admin_request("GET", "/api/admin/schedule-triggers", cookies=cookies),
                         "GET", "/api/admin/schedule-triggers")
    row = json.loads(listed["body"])["schedules"][0]
    assert row["summary"] == "every weekday at 09:00 UTC"
    assert len(row["next_runs"]) == 5
    assert row["health"]["state"] in ("waiting", "attention")


def test_admin_schedule_actions_require_signin():
    path = "/api/admin/schedule-triggers/digest/run"
    response = admin.route(admin_request("POST", path, body={}), "POST", path)
    assert response["statusCode"] == 401


# --- CLI routes (/api/agent) --------------------------------------------------------

def test_agent_routes_dispatch_actions_and_upcoming():
    table = Table([{"schedule_id": "digest", "expression": "rate(1 day)", "enabled": True}])
    with patch("src.dapier.api.agent.require_operator", return_value=("sub-1", None)), \
         patch.object(schedule_triggers, "get_table", return_value=table), \
         patch.object(schedule_triggers, "sync_rule"), \
         patch.object(schedule_triggers, "load_workflows", return_value=[]):
        paused = agent_api.route({"headers": {}}, "POST",
                                 "/api/agent/schedule-triggers/digest/pause")
        upcoming = agent_api.schedule_upcoming_api(
            {"headers": {}, "queryStringParameters": {"hours": "24"}})
    assert paused["statusCode"] == 200
    assert json.loads(paused["body"])["enabled"] is False
    assert upcoming["statusCode"] == 200
    assert json.loads(upcoming["body"])["upcoming"] == []  # paused: nothing upcoming


def test_agent_routes_refuse_non_operators():
    denied = {"statusCode": 403, "body": "{}"}
    with patch("src.dapier.api.agent.require_operator", return_value=(None, denied)):
        assert agent_api.schedule_action_api({"headers": {}}, "digest", "run") is denied
        assert agent_api.schedule_upcoming_api({"headers": {}}) is denied


# --- the CLI ------------------------------------------------------------------------

SHOWN = {
    "schedule_id": "digest", "expression": "cron(0 9 ? * MON-FRI *)",
    "summary": "every weekday at 09:00 UTC", "enabled": True,
    "next_runs": ["2026-10-09T09:00:00Z"], "approximate": False,
    "workflows": [{"id": "digest-flow", "name": "digest-flow", "enabled": True}],
    "health": {"state": "attention", "reason": "Last fired 3 days ago; expected every day."},
    "fires": [{"at": "2026-10-05T09:00:00+00:00", "outcome": "ran", "event_id": "e1",
               "workflows": ["digest-flow"],
               "runs": [{"workflow_id": "digest-flow", "run_id": "digest-flow:e1"}]}],
}


def fake_api(monkeypatch, responses):
    calls = []

    def fake_call(api_url, method, path, body=None, **kwargs):
        calls.append((method, path))
        return responses[(method, path)]

    monkeypatch.setattr(commands.api, "call", fake_call)
    return calls


def test_cli_list_and_show_render_health_and_history(monkeypatch, capsys):
    fake_api(monkeypatch, {("GET", "/api/agent/schedule-triggers"): {"schedules": [SHOWN]}})
    assert commands.schedules_list("https://api.test") == 0
    out = capsys.readouterr().out
    assert "NEEDS ATTENTION" in out and "every weekday at 09:00 UTC" in out
    assert "Last fired 3 days ago" in out and "next Fri 09 Oct 09:00" in out
    assert commands.schedules_show("https://api.test", "digest") == 0
    out = capsys.readouterr().out
    assert "Runs:      digest-flow" in out
    assert "Mon 05 Oct 09:00" in out and "run digest-flow:e1" in out
    assert commands.schedules_show("https://api.test", "nope") == 1


def test_cli_upcoming_run_pause_resume(monkeypatch, capsys):
    calls = fake_api(monkeypatch, {
        ("GET", "/api/agent/schedule-triggers/upcoming?hours=168"): {
            "upcoming": [{"at": "2026-10-09T09:00:00Z", "schedule_id": "digest",
                          "workflows": ["digest-flow"]}],
            "frequent": [{"schedule_id": "tick", "summary": "every 5 minutes",
                          "count": 2016, "workflows": []}]},
        ("POST", "/api/agent/schedule-triggers/digest/run"): {
            "schedule_id": "digest", "event_id": "digest-manual-1",
            "workflows": ["digest-flow"],
            "runs": [{"workflow_id": "digest-flow", "run_id": "digest-flow:digest-manual-1"}]},
        ("POST", "/api/agent/schedule-triggers/digest/pause"): {
            "schedule_id": "digest", "changed": True},
        ("POST", "/api/agent/schedule-triggers/digest/resume"): {
            "schedule_id": "digest", "changed": False},
    })
    assert commands.schedules_upcoming("https://api.test", 168) == 0
    assert commands.schedules_run("https://api.test", "digest") == 0
    assert commands.schedules_pause("https://api.test", "digest") == 0
    assert commands.schedules_resume("https://api.test", "digest") == 0
    out = capsys.readouterr().out
    assert "Next 7 days (UTC):" in out and "Fri 09 Oct" in out and "-> digest-flow" in out
    assert "tick: every 5 minutes (2016 fires)" in out
    assert "dapier runs show digest-flow:digest-manual-1" in out
    assert "'digest' paused" in out and "already resumed" in out
    assert [path for _, path in calls][1:] == [
        "/api/agent/schedule-triggers/digest/run",
        "/api/agent/schedule-triggers/digest/pause",
        "/api/agent/schedule-triggers/digest/resume"]


def test_cli_parser_wires_the_new_subcommands(monkeypatch):
    seen = []
    for name in ("schedules_show", "schedules_upcoming", "schedules_run",
                 "schedules_pause", "schedules_resume"):
        monkeypatch.setattr(commands, name,
                            lambda *args, _n=name: seen.append((_n, args[1:])) or 0)
    for argv in (["schedules", "show", "digest"], ["schedules", "upcoming", "--days", "7"],
                 ["schedules", "upcoming"], ["schedules", "run", "digest"],
                 ["schedules", "pause", "digest"], ["schedules", "resume", "digest"]):
        assert main.main(argv) == 0
    assert seen == [
        ("schedules_show", ("digest", False)),
        ("schedules_upcoming", (168, False)),
        ("schedules_upcoming", (24, False)),
        ("schedules_run", ("digest", False)),
        ("schedules_pause", ("digest", False)),
        ("schedules_resume", ("digest", False)),
    ]

