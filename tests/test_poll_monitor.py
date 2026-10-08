"""Poll health monitor: the status row each fire records, the health word,
the workflows a poll starts, Poll now / pause / resume / reset, the items
polls picked up, and the console + CLI surfaces that reach them."""
import json
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import boto3
import pytest
from moto import mock_aws

from src.dapier.api.admin import dispatch as admin_dispatch
from src.dapier.api.admin import routes as admin_routes
from src.dapier.triggers import poll_status, poll_triggers
from src.dapier.triggers.email_triggers import TriggerError

from tests.test_poll_triggers import FakeEvents, FakePollTable


@pytest.fixture
def dynamo(monkeypatch):
    with mock_aws():
        resource = boto3.resource("dynamodb", region_name="eu-west-1")
        cursors = resource.create_table(
            TableName="cursors",
            KeySchema=[{"AttributeName": "cursor_id", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "cursor_id", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST")
        inbox = resource.create_table(
            TableName="inbox",
            KeySchema=[{"AttributeName": "inbox_id", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "inbox_id", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST")
        monkeypatch.setenv("CURSORS_TABLE", "cursors")
        monkeypatch.setenv("TRIGGER_INBOX_TABLE", "inbox")
        yield {"cursors": cursors, "inbox": inbox}


def poll(**overrides):
    item = {"poll_id": "orders", "enabled": True, "expression": "rate(5 minutes)",
            "source": "http", "url": "https://example.test/orders", "method": "GET",
            "cursor_mode": "watermark", "id_path": "id", "max_items": 25, "headers": {}}
    item.update(overrides)
    return item


# --- status row + health -------------------------------------------------------

def test_status_records_checks_failures_and_new_items(dynamo):
    poll_status.record("orders", found=0)
    assert poll_status.get("orders")["failures"] == 0
    assert "last_new_at" not in poll_status.get("orders")

    poll_status.record("orders", error=RuntimeError("HTTP 500"))
    poll_status.record("orders", error=RuntimeError("HTTP 502"), reason="manual")
    status = poll_status.get("orders")
    assert status["failures"] == 2
    assert status["last_error"] == "HTTP 502"
    assert status["last_reason"] == "manual"

    poll_status.record("orders", found=3)
    status = poll_status.get("orders")
    assert status["failures"] == 0
    assert status["last_new_count"] == 3 and status["last_found"] == 3
    assert status["last_error"] == "HTTP 502"  # the last failure stays visible

    poll_status.drop("orders")
    assert poll_status.get("orders") == {}


def test_status_record_never_raises_without_a_table(monkeypatch):
    monkeypatch.delenv("CURSORS_TABLE", raising=False)
    assert poll_status.record("orders", found=1) is None
    assert poll_status.get("orders") == {}


def test_health_words():
    now = datetime(2026, 10, 8, 12, 0, tzinfo=timezone.utc)
    recent = (now - timedelta(minutes=4)).isoformat()
    stale = (now - timedelta(minutes=30)).isoformat()
    assert poll_status.health(poll(enabled=False), {}, now=now) == "paused"
    assert poll_status.health(poll(), {}, now=now) == "waiting"
    assert poll_status.health(poll(), {"last_checked_at": recent, "failures": 1}, now=now) == "failing"
    assert poll_status.health(poll(), {"last_checked_at": stale, "failures": 0}, now=now) == "late"
    assert poll_status.health(poll(), {"last_checked_at": recent, "failures": 0}, now=now) == "ok"
    # cron schedules have no fixed interval, so they are never "late"
    assert poll_status.health(poll(expression="cron(0 9 * * ? *)"),
                              {"last_checked_at": stale}, now=now) == "ok"
    assert poll_status.interval_seconds("rate(1 hour)") == 3600


# --- fire() records health --------------------------------------------------------

def test_fire_records_a_clean_check_with_what_it_found(dynamo):
    with patch.object(poll_triggers, "get_item", return_value=poll()), \
         patch.object(poll_triggers, "fetch_page_response",
                      return_value=([{"id": 1}, {"id": 2}], None)), \
         patch("src.dapier.engine.execute", return_value=["wf"]):
        result = poll_triggers.fire("orders")

    assert result == {"poll": "orders", "fired": 2}
    status = poll_status.get("orders")
    assert status["last_found"] == 2 and status["failures"] == 0
    assert status["last_reason"] == "schedule"


def test_fire_records_a_failed_fetch_and_reraises(dynamo):
    with patch.object(poll_triggers, "get_item", return_value=poll()), \
         patch.object(poll_triggers, "fetch_page_response",
                      side_effect=RuntimeError("poll fetch returned HTTP 503")):
        with pytest.raises(RuntimeError):
            poll_triggers.fire("orders", reason="manual")

    status = poll_status.get("orders")
    assert status["failures"] == 1
    assert status["last_error"] == "poll fetch returned HTTP 503"
    assert status["last_reason"] == "manual"


def test_fire_records_an_item_failure(dynamo):
    with patch.object(poll_triggers, "get_item", return_value=poll()), \
         patch.object(poll_triggers, "fetch_page_response", return_value=([{"id": 7}], None)), \
         patch("src.dapier.engine.execute", side_effect=RuntimeError("slack down")), \
         patch("src.dapier.engine.notify.notify_failure"):
        result = poll_triggers.fire("orders")

    assert result == {"poll": "orders", "fired": 0}
    assert poll_status.get("orders")["last_error"] == "item 7: slack down"


# --- workflows a poll starts ---------------------------------------------------------

def test_bound_workflows_follow_the_poll_filter():
    workflows = [
        {"id": "ship", "trigger": {"connector": "poll", "event": "item.new",
                                   "filters": {"poll": {"equals": "orders"}}}},
        {"id": "other", "trigger": {"connector": "poll", "event": "item.new",
                                    "filters": {"poll": {"equals": "refunds"}}}},
        {"id": "any-poll", "enabled": False,
         "trigger": {"connector": "poll", "event": "item.new"}},
        {"id": "email", "trigger": {"connector": "email", "event": "received"}},
    ]
    found = poll_triggers.bound_workflows(poll(), workflows)
    assert [(flow["id"], flow["enabled"]) for flow in found] == [("ship", True), ("any-poll", False)]


def test_list_carries_health_workflows_and_reset_modes(dynamo):
    polls = FakePollTable([poll(), poll(poll_id="feed", enabled=False)])
    poll_status.record("orders", found=1)
    poll_triggers.put_cursor("orders", "2026-10-01T00:00:00Z")
    workflows = [{"id": "ship", "trigger": {"connector": "poll", "event": "item.new",
                                            "filters": {"poll": "orders"}}}]

    status, payload = poll_triggers.api_list(table_ref=polls, workflows=workflows)

    assert status == 200
    rows = {row["poll_id"]: row for row in payload["polls"]}
    assert rows["orders"]["health"] == "ok"
    assert rows["orders"]["status"]["last_found"] == 1
    assert [flow["id"] for flow in rows["orders"]["workflows"]] == ["ship"]
    assert rows["orders"]["reset_modes"] == ["now", "date"]
    assert rows["feed"]["health"] == "paused"
    assert rows["feed"]["reset_modes"] == ["now"]


# --- actions ----------------------------------------------------------------------------

def test_pause_and_resume_move_the_flag_and_the_rule():
    polls, events = FakePollTable([poll()]), FakeEvents()

    status, payload = poll_triggers.api_set_enabled(
        "orders", False, "op", table_ref=polls, events_client=events, target_arn="arn:w")
    assert (status, payload["enabled"]) == (200, False)
    assert polls.items["orders"]["enabled"] is False
    assert events.rules["dapier-poll-orders"]["State"] == "DISABLED"

    poll_triggers.api_set_enabled("orders", True, "op", table_ref=polls,
                                  events_client=events, target_arn="arn:w")
    assert events.rules["dapier-poll-orders"]["State"] == "ENABLED"

    with pytest.raises(TriggerError):
        poll_triggers.api_set_enabled("missing", True, "op", table_ref=polls,
                                      events_client=events, target_arn="arn:w")


class FakeQueue:
    def __init__(self):
        self.sent = []

    def send_message(self, **kwargs):
        self.sent.append(kwargs)


def test_poll_now_queues_one_check(monkeypatch):
    monkeypatch.setenv("EVENT_QUEUE_URL", "https://sqs.example.test/events")
    queue = FakeQueue()

    status, payload = poll_triggers.api_check("orders", table_ref=FakePollTable([poll()]),
                                              queue=queue)

    assert (status, payload["accepted"]) == (202, True)
    assert json.loads(queue.sent[0]["MessageBody"]) == {"poll_now": {"poll_id": "orders"}}


def test_poll_now_refuses_a_paused_poll(monkeypatch):
    monkeypatch.setenv("EVENT_QUEUE_URL", "https://sqs.example.test/events")
    queue = FakeQueue()
    status, _ = poll_triggers.api_check("orders", table_ref=FakePollTable([poll(enabled=False)]),
                                        queue=queue)
    assert status == 409 and queue.sent == []


def test_worker_runs_poll_now_messages_without_retrying_failures():
    from src.dapier.engine import worker

    record = {"messageId": "m1", "body": json.dumps({"poll_now": {"poll_id": "orders"}})}
    with patch.object(poll_triggers, "fire", return_value={"fired": 0}) as fire:
        result = worker.handler({"Records": [record]}, None)
    fire.assert_called_once_with("orders", reason="manual")
    assert result == {"batchItemFailures": []}

    with patch.object(poll_triggers, "fire", side_effect=RuntimeError("HTTP 500")), \
         patch.object(worker, "notify_failure") as notify:
        result = worker.handler({"Records": [record]}, None)
    assert result == {"batchItemFailures": []}
    notify.assert_called_once()


def test_reset_to_now_jumps_a_watermark_past_waiting_items(dynamo):
    polls = FakePollTable([poll()])
    poll_triggers.put_cursor("orders", "3")
    with patch.object(poll_triggers, "fetch_page_response",
                      return_value=([{"id": 2}, {"id": 5}, {"id": 9}], None)):
        status, payload = poll_triggers.api_reset("orders", {"to": "now"}, "op", table_ref=polls)

    assert status == 200
    assert payload["skipped"] == 2 and payload["cursor"] == "9"
    assert poll_triggers.get_cursor("orders") == "9"


def test_reset_to_now_marks_a_page_seen_and_parks_the_cursor(dynamo):
    from src.dapier.triggers import seen

    polls = FakePollTable([poll(cursor_mode="next_cursor", source="rss")])
    with patch.object(poll_triggers, "fetch_page_response",
                      return_value=([{"id": "a"}, {"id": "b"}], "b")):
        status, payload = poll_triggers.api_reset("orders", {"to": "now"}, "op", table_ref=polls)

    assert (status, payload["skipped"], payload["cursor"]) == (200, 2, "b")
    remembered = seen.load("poll#orders")
    assert poll_triggers._stable_event_id("orders", "a") in remembered


def test_reset_to_a_date_sets_a_timestamp_position(dynamo):
    from src.dapier.triggers import seen

    polls = FakePollTable([poll(cursor_mode="next_cursor", source="rss")])
    poll_triggers.put_cursor("orders", "2026-10-08T07:20:58.154Z")
    seen.remember("poll#orders", "orders-x")

    status, payload = poll_triggers.api_reset(
        "orders", {"to": "date", "date": "2026-10-01"}, "op", table_ref=polls)

    assert (status, payload["cursor"]) == (200, "2026-10-01T00:00:00Z")
    assert poll_triggers.get_cursor("orders") == "2026-10-01T00:00:00Z"
    assert seen.load("poll#orders") == {}  # those items may run again


def test_reset_to_a_date_needs_a_timestamp_position(dynamo):
    polls = FakePollTable([poll()])
    poll_triggers.put_cursor("orders", "42")
    with pytest.raises(TriggerError, match="reset it to now"):
        poll_triggers.api_reset("orders", {"to": "date", "date": "2026-10-01"}, "op",
                                table_ref=polls)
    with pytest.raises(TriggerError):
        poll_triggers.api_reset("orders", {"to": "yesterday"}, "op", table_ref=polls)


def test_activity_lists_poll_items_with_their_runs(dynamo):
    inbox = dynamo["inbox"]
    inbox.put_item(Item={"inbox_id": "orders-1", "connector": "poll", "event": "item.new",
                         "source": "orders", "received_at": "2026-10-08T10:00:00+00:00",
                         "status": "matched", "matched": ["ship"],
                         "data": {"poll": "orders", "item_id": "1", "title": "Order #1"}})
    inbox.put_item(Item={"inbox_id": "orders-2", "connector": "poll", "event": "item.new",
                         "source": "orders", "received_at": "2026-10-08T11:00:00+00:00",
                         "status": "unmatched", "matched": [],
                         "data": {"poll": "orders", "item_id": "2"}})
    inbox.put_item(Item={"inbox_id": "feed-1", "connector": "rss", "event": "item.new",
                         "source": "feed", "received_at": "2026-10-08T12:00:00+00:00",
                         "status": "matched", "matched": ["blog"],
                         "data": {"poll": "feed", "item_id": "x"}})
    inbox.put_item(Item={"inbox_id": "hook-1", "connector": "webhook", "event": "received",
                         "received_at": "2026-10-08T13:00:00+00:00", "status": "matched",
                         "matched": [], "data": {"hook": "x"}})
    polls = FakePollTable([poll(), poll(poll_id="feed")])

    status, payload = poll_triggers.api_activity(table_ref=polls)
    assert status == 200
    assert [item["inbox_id"] for item in payload["items"]] == ["feed-1", "orders-2", "orders-1"]

    status, payload = poll_triggers.api_activity("orders", table_ref=polls)
    items = payload["items"]
    assert [item["inbox_id"] for item in items] == ["orders-2", "orders-1"]
    assert items[1]["runs"] == ["ship:orders-1"]
    assert items[1]["title"] == "Order #1" and items[0]["title"] == "2"

    with pytest.raises(TriggerError):
        poll_triggers.api_activity("missing", table_ref=polls)


def test_delete_drops_the_status_row(dynamo):
    polls = FakePollTable([poll()])
    poll_status.record("orders", found=1)
    poll_triggers.api_delete("orders", "op", table_ref=polls, events_client=FakeEvents())
    assert poll_status.get("orders") == {}


# --- console routes ------------------------------------------------------------------------

def admin_event(method, path, body=None, query=None):
    event = {"requestContext": {"http": {"method": method, "path": path}},
             "headers": {"host": "dapier.example.test"}}
    if body is not None:
        event["body"] = json.dumps(body)
    if query is not None:
        event["queryStringParameters"] = query
    return event


def route_admin(method, path, body=None, query=None):
    with patch("src.dapier.auth.session._audit_event") as audit_event:
        response = admin_dispatch._route_triggers(
            admin_event(method, path, body, query), method, path, {}, "op@example.test")
    return response, audit_event


def test_admin_routes_reach_the_poll_actions(monkeypatch):
    calls = []
    monkeypatch.setattr(poll_triggers, "api_check",
                        lambda name: calls.append(("check", name)) or (202, {"accepted": True}))
    monkeypatch.setattr(poll_triggers, "api_set_enabled",
                        lambda name, enabled, op: calls.append(("enabled", name, enabled))
                        or (200, {"enabled": enabled}))
    monkeypatch.setattr(poll_triggers, "api_reset",
                        lambda name, body, op: calls.append(("reset", name, body))
                        or (200, {"cursor": "9"}))
    monkeypatch.setattr(poll_triggers, "api_show",
                        lambda name: (200, {"poll": {"poll_id": name}}))
    monkeypatch.setattr(poll_triggers, "api_activity",
                        lambda name, limit: (200, {"items": [], "poll": name}))

    response, audit_event = route_admin("POST", "/api/admin/poll-triggers/orders/check")
    assert response["statusCode"] == 202
    assert audit_event.call_args[0] == ("orders", "poll-trigger.check", "op@example.test")
    assert route_admin("POST", "/api/admin/poll-triggers/orders/pause")[0]["statusCode"] == 200
    assert route_admin("POST", "/api/admin/poll-triggers/orders/resume")[0]["statusCode"] == 200
    assert route_admin("POST", "/api/admin/poll-triggers/orders/reset",
                       body={"to": "now"})[0]["statusCode"] == 200
    assert calls == [("check", "orders"), ("enabled", "orders", False),
                     ("enabled", "orders", True), ("reset", "orders", {"to": "now"})]
    shown, _ = route_admin("GET", "/api/admin/poll-triggers/orders")
    assert json.loads(shown["body"]) == {"poll": {"poll_id": "orders"}}
    activity, _ = route_admin("GET", "/api/admin/poll-triggers/activity", query={"name": "orders"})
    assert json.loads(activity["body"])["poll"] == "orders"


def test_admin_route_maps_errors():
    with patch.object(poll_triggers, "get_table", return_value=FakePollTable([])):
        response, _ = route_admin("POST", "/api/admin/poll-triggers/missing/pause")
    assert response["statusCode"] == 404
    with patch.object(poll_triggers, "api_reset", side_effect=TriggerError("to must be 'now' or 'date'")):
        response, _ = route_admin("POST", "/api/admin/poll-triggers/orders/reset", body={})
    assert response["statusCode"] == 400
    with patch.object(poll_triggers, "api_reset", side_effect=RuntimeError("rss fetch failed")):
        response, _ = route_admin("POST", "/api/admin/poll-triggers/orders/reset",
                                  body={"to": "now"})
    assert response["statusCode"] == 502


# --- agent API (the CLI's routes) ----------------------------------------------------------

def test_agent_poll_routes_require_an_operator(monkeypatch):
    from src.dapier.api import agent as agent_api
    from tests.test_agent_api import configure, event

    configure(monkeypatch, claims={"sub": "agent-1", "email": "agent@example.test"})
    monkeypatch.setenv("OPERATOR_EMAILS", "op@datatalks.club")
    for method, path in (("GET", "/api/agent/poll-triggers/orders"),
                         ("GET", "/api/agent/poll-triggers/activity"),
                         ("POST", "/api/agent/poll-triggers/orders/check")):
        assert agent_api.route(event(), method, path)["statusCode"] == 403


def test_agent_poll_routes_share_the_domain_calls(monkeypatch):
    from src.dapier.api import agent as agent_api
    from tests.test_agent_api import configure, event

    configure(monkeypatch, claims={"sub": "op-1", "email": "op@datatalks.club"})
    monkeypatch.setenv("OPERATOR_EMAILS", "op@datatalks.club")
    calls = []
    monkeypatch.setattr(poll_triggers, "api_check",
                        lambda name: calls.append(("check", name)) or (202, {"accepted": True}))
    monkeypatch.setattr(poll_triggers, "api_set_enabled",
                        lambda name, enabled, op: calls.append(("enabled", name, enabled))
                        or (200, {}))
    monkeypatch.setattr(poll_triggers, "api_reset",
                        lambda name, body, op: calls.append(("reset", name, body)) or (200, {}))
    monkeypatch.setattr(poll_triggers, "api_show", lambda name: (200, {"poll": {"poll_id": name}}))
    monkeypatch.setattr(poll_triggers, "api_activity",
                        lambda name, limit: calls.append(("activity", name, limit))
                        or (200, {"items": []}))

    assert agent_api.route(event({}), "POST", "/api/agent/poll-triggers/orders/check")["statusCode"] == 202
    agent_api.route(event({}), "POST", "/api/agent/poll-triggers/orders/pause")
    agent_api.route(event({}), "POST", "/api/agent/poll-triggers/orders/resume")
    agent_api.route(event({"to": "date", "date": "2026-10-01"}), "POST",
                    "/api/agent/poll-triggers/orders/reset")
    agent_api.route(event(query={"name": "orders", "limit": "5"}), "GET",
                    "/api/agent/poll-triggers/activity")
    shown = agent_api.route(event(), "GET", "/api/agent/poll-triggers/orders")
    assert json.loads(shown["body"])["poll"]["poll_id"] == "orders"
    assert calls == [("check", "orders"), ("enabled", "orders", False),
                     ("enabled", "orders", True),
                     ("reset", "orders", {"to": "date", "date": "2026-10-01"}),
                     ("activity", "orders", "5")]


# --- CLI ----------------------------------------------------------------------------------------

def test_cli_poll_commands_hit_the_agent_routes(monkeypatch, capsys):
    from dapier_cli import commands

    calls = []

    def fake_call(api_url, method, path, body=None, **kwargs):
        calls.append((method, path, body))
        if path.endswith("/reset"):
            return {"cursor": "2026-10-01T00:00:00Z", "skipped": 4}
        if "/activity" in path:
            return {"items": [{"inbox_id": "orders-1", "poll": "orders", "status": "matched",
                               "received_at": "2026-10-08T10:00:00+00:00", "title": "Order #1",
                               "runs": ["ship:orders-1"]}]}
        if path == "/api/agent/poll-triggers/orders":
            return {"poll": {"poll_id": "orders", "health": "failing", "expression": "rate(5 minutes)",
                             "url": "https://example.test/orders",
                             "workflows": [{"id": "ship", "enabled": True}],
                             "status": {"last_checked_at": "2026-10-08T10:00:00+00:00",
                                        "failures": 2, "last_error": "HTTP 503"},
                             "reset_modes": ["now"]}}
        return {"accepted": True}

    monkeypatch.setattr(commands.api, "call", fake_call)
    base = "https://api.example.test"
    assert commands.polls_show(base, "orders") == 0
    assert commands.polls_activity(base, "orders", limit=5) == 0
    assert commands.polls_check(base, "orders") == 0
    assert commands.polls_set_enabled(base, "orders", False) == 0
    assert commands.polls_set_enabled(base, "orders", True) == 0
    assert commands.polls_reset(base, "orders") == 0
    assert commands.polls_reset(base, "orders", date="2026-10-01") == 0

    assert [(method, path) for method, path, _ in calls] == [
        ("GET", "/api/agent/poll-triggers/orders"),
        ("GET", "/api/agent/poll-triggers/activity?limit=5&name=orders"),
        ("POST", "/api/agent/poll-triggers/orders/check"),
        ("POST", "/api/agent/poll-triggers/orders/pause"),
        ("POST", "/api/agent/poll-triggers/orders/resume"),
        ("POST", "/api/agent/poll-triggers/orders/reset"),
        ("POST", "/api/agent/poll-triggers/orders/reset"),
    ]
    assert calls[5][2] == {"to": "now"}
    assert calls[6][2] == {"to": "date", "date": "2026-10-01"}
    out = capsys.readouterr().out
    assert "health: failing" in out and "last error" in out and "HTTP 503" in out
    assert "starts: ship" in out
    assert "Order #1" in out and "run: ship:orders-1" in out
    assert "4 waiting item(s) skipped" in out


def test_cli_list_shows_health_and_workflows(monkeypatch, capsys):
    from dapier_cli import commands

    monkeypatch.setattr(commands.api, "call", lambda *args, **kwargs: {"polls": [
        {"poll_id": "orders", "health": "waiting", "expression": "rate(5 minutes)",
         "url": "https://example.test/orders", "status": {}, "workflows": []}]})
    assert commands.polls_list("https://api.example.test") == 0
    out = capsys.readouterr().out
    assert "not checked yet" in out and "starts: no workflow" in out


def test_cli_parser_routes_poll_subcommands(monkeypatch):
    from dapier_cli import commands, main

    seen = []
    monkeypatch.setattr(commands, "polls_reset",
                        lambda api_url, name, date, debug=False: seen.append(("reset", name, date)) or 0)
    monkeypatch.setattr(commands, "polls_set_enabled",
                        lambda api_url, name, enabled, debug=False: seen.append(("enabled", name, enabled)) or 0)
    monkeypatch.setattr(commands, "polls_check",
                        lambda api_url, name, debug=False: seen.append(("check", name)) or 0)
    monkeypatch.setattr(commands, "polls_activity",
                        lambda api_url, name, limit, debug=False: seen.append(("activity", name, limit)) or 0)

    assert main.main(["polls", "reset", "orders", "--now"]) == 0
    assert main.main(["polls", "reset", "orders", "--from", "2026-10-01"]) == 0
    assert main.main(["polls", "pause", "orders"]) == 0
    assert main.main(["polls", "resume", "orders"]) == 0
    assert main.main(["polls", "check", "orders"]) == 0
    assert main.main(["polls", "activity"]) == 0
    assert seen == [("reset", "orders", None), ("reset", "orders", "2026-10-01"),
                    ("enabled", "orders", False), ("enabled", "orders", True),
                    ("check", "orders"), ("activity", None, 25)]


def test_cli_watches_names_every_provider_target():
    from dapier_cli.commands import polls as poll_commands

    assert poll_commands._poll_target({"source": "dropbox.files", "path": "/Invoices"}) == "dropbox.files /Invoices"
    assert poll_commands._poll_target({"source": "rss", "url": "https://x.test/feed"}) == "rss https://x.test/feed"
