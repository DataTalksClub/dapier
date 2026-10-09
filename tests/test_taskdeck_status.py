"""The taskdeck status contract: GET /internal/ops/status.

Dapier publishes the payload the cross-project console already reads from
Relay, projected out of the schedule view (schedule_triggers.public_view)
rather than out of a second reading of the same data. Covered here: the row
mapping, the health verdict carried through whole, the fields Dapier refuses
to invent, and the bearer gate that answers 404 rather than 401.
"""

import json
from datetime import datetime, timedelta, timezone

from dapier_cli import __version__ as app_version
from src.dapier.api import router
from src.dapier.triggers import taskdeck_status

NOW = datetime(2026, 10, 8, 15, 30, tzinfo=timezone.utc)  # a Thursday
WEEKDAYS = "cron(0 9 ? * MON-FRI *)"
HOURLY = "cron(0 * * * ? *)"


def iso(moment):
    return moment.isoformat()


class Table:
    """In-memory SCHEDULE_TRIGGERS_TABLE (the scan surface load_items reads)."""

    def __init__(self, items=()):
        self.items = {item["schedule_id"]: dict(item) for item in items}

    def scan(self, **kwargs):
        return {"Items": [dict(item) for item in self.items.values()]}


def listening(name, workflow_id="digest-flow", enabled=True):
    return {"id": workflow_id, "enabled": enabled, "actions": [],
            "trigger": {"connector": "schedule", "event": "schedule.triggered",
                        "filters": {"schedule": {"equals": name}}}}


def fired(name, moment, outcome="ran", **fire):
    return {"schedule_id": name, "expression": WEEKDAYS, "enabled": True,
            "updated_at": iso(NOW - timedelta(days=5)),
            "fires": [{"at": iso(moment), "outcome": outcome,
                       "event_id": f"{name}-1", "workflows": ["digest-flow"], **fire}]}


def collect(table=None, workflows=None):
    return taskdeck_status.collect_status(
        table, now=NOW, workflows=[listening("digest")] if workflows is None else workflows)


def request(method="GET", token="console-token"):
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    return {"requestContext": {"http": {"method": method, "path": "/internal/ops/status"}},
            "headers": headers, "cookies": []}


def _gateway_routes():
    """The API's route events plus its stage CORS allow-headers, read the way
    test_template_alarms reads the template (its CloudFormation tag loader is
    registered at import time)."""
    import yaml

    from tests import test_template_alarms  # noqa: F401 — registers the !Tag loader

    with open("template.yaml", encoding="utf-8") as handle:
        resources = yaml.load(handle, Loader=yaml.SafeLoader)["Resources"]
    events = resources["IngressFunction"]["Properties"]["Events"].values()
    headers = resources["Api"]["Properties"]["CorsConfiguration"]["AllowHeaders"]
    return ([str(event["Properties"]["Path"]).lower() for event in events], headers)


# --- the contract rows ------------------------------------------------------------

def test_each_schedule_becomes_a_contract_row():
    table = Table([fired("digest", NOW - timedelta(hours=1))])
    rows = collect(table)["schedules"]
    assert rows == [{
        "name": "digest",
        "cron": WEEKDAYS,
        "next_run": "2026-10-09T09:00:00Z",
        "last_run": iso(NOW - timedelta(hours=1)),
        "last_success": iso(NOW - timedelta(hours=1)),
        # One weekday interval plus the grace EventBridge gets before a fire
        # counts as missed, so the console's overdue flag agrees with
        # Dapier's own "attention" verdict instead of firing early.
        "max_age_s": 86400 + 600,
        "enabled": True,
        "health": {"state": "ok", "reason": "Last fired 1 hour ago, on time."},
    }]


def test_a_paused_schedule_has_no_next_run():
    item = {**fired("digest", NOW - timedelta(hours=1)), "enabled": False}
    row = collect(Table([item]))["schedules"][0]
    # The rule is disabled, so there is no next fire to report — null, not
    # the fire it would have had if it were running.
    assert row["next_run"] is None
    assert row["enabled"] is False
    assert row["health"]["state"] == "paused"


def test_a_manual_fire_is_not_a_success():
    """Run now runs the workflows, but it says nothing about the rule: a
    schedule stopped for two days must not read as current because an
    operator fired it by hand."""
    item = fired("digest", NOW - timedelta(days=2))
    item["fires"].insert(0, {"at": iso(NOW - timedelta(minutes=1)),
                             "outcome": "ran", "manual": True})
    row = collect(Table([item]))["schedules"][0]
    assert row["last_success"] == iso(NOW - timedelta(days=2))
    assert row["last_run"] == iso(NOW - timedelta(days=2))


def test_a_failed_fire_walks_back_to_the_last_success():
    item = fired("digest", NOW - timedelta(minutes=30), outcome="failed", error="boom")
    item["fires"].append({"at": iso(NOW - timedelta(hours=25)), "outcome": "ran",
                          "event_id": "digest-0", "workflows": ["digest-flow"]})
    row = collect(Table([item]))["schedules"][0]
    assert row["last_run"] == iso(NOW - timedelta(minutes=30))
    assert row["last_success"] == iso(NOW - timedelta(hours=25))


def test_a_schedule_with_no_repeating_interval_has_no_max_age():
    item = {"schedule_id": "yearly", "expression": "cron(0 9 1 1 ? 2030)",
            "enabled": True, "updated_at": iso(NOW)}
    row = collect(Table([item]))["schedules"][0]
    assert row["max_age_s"] is None


def test_a_rate_schedule_ages_against_its_exact_interval():
    """A rate's gap is exact rather than typical: two hours plus the same ten
    minutes EventBridge gets before a fire counts as missed."""
    item = {"schedule_id": "hourly", "expression": "rate(2 hours)",
            "enabled": True, "updated_at": iso(NOW - timedelta(hours=1))}
    row = collect(Table([item]), workflows=[listening("hourly")])["schedules"][0]
    assert row["max_age_s"] == 2 * 3600 + 600
    assert row["next_run"] == "2026-10-08T16:30:00Z"


def test_an_unreadable_expression_degrades_to_nulls_not_a_500():
    """Shape-valid on save but unreadable on read (cron(99 99 ...) passes the
    save pattern yet names no minute): the row reports no next fire and no
    age to check against, and the list still renders around it."""
    item = {"schedule_id": "broken", "expression": "cron(99 99 ? * * *)",
            "enabled": True, "updated_at": iso(NOW)}
    row = collect(Table([item]), workflows=[listening("broken")])["schedules"][0]
    assert row["next_run"] is None
    assert row["max_age_s"] is None
    assert row["last_run"] is None
    assert row["last_success"] is None


# --- Dapier's own health verdict --------------------------------------------------

def test_health_is_carried_through_whole():
    """The reason is not decoration: "the last fire failed" and "nothing
    listens to it" send an operator to different places."""
    item = fired("digest", NOW - timedelta(minutes=30), outcome="failed", error="boom")
    assert collect(Table([item]))["schedules"][0]["health"] == {
        "state": "attention", "reason": "The last fire failed: boom"}


def test_schedules_attention_counts_the_rows_that_need_a_human():
    loud = {**fired("loud", NOW - timedelta(minutes=30), outcome="failed", error="boom")}
    # Silent for three days on an hourly schedule.
    still = {"schedule_id": "still", "expression": HOURLY, "enabled": True,
             "updated_at": iso(NOW - timedelta(days=5)),
             "fires": [{"at": iso(NOW - timedelta(days=3)), "outcome": "ran",
                        "event_id": "still-1", "workflows": ["digest-flow"]}]}
    quiet = {**fired("quiet", NOW - timedelta(minutes=5)), "expression": HOURLY}
    payload = collect(Table([quiet, still, loud]),
                      workflows=[listening("loud"), listening("quiet"), listening("still")])
    states = {row["name"]: row["health"]["state"] for row in payload["schedules"]}
    assert states == {"loud": "attention", "still": "attention", "quiet": "ok"}
    assert payload["schedules_attention"] == 2


# --- what Dapier does not know ----------------------------------------------------

def test_an_unconfigured_schedule_table_is_an_empty_list(monkeypatch):
    """A deployment with no schedule table has nothing to report, which is an
    answer rather than an error: the console shows an empty schedule list
    instead of a broken panel."""
    monkeypatch.delenv("SCHEDULE_TRIGGERS_TABLE", raising=False)
    payload = collect()
    assert payload["schedules"] == []
    assert payload["schedules_attention"] == 0


def test_worker_liveness_and_queue_depth_are_null_not_zero():
    """There is no heartbeat to age and no consumer-lag signal to read, so a
    consumer must be able to tell "unknown" from "fine"."""
    payload = collect()
    assert payload["worker"] == {
        "mode": "lambda", "last_seen": None, "healthy": None, "stalled_runs": None}
    assert payload["queue"]["depth"] is None
    assert payload["queue"]["oldest_pending_age_s"] is None


def test_the_queue_is_named_from_the_deployment(monkeypatch):
    monkeypatch.setenv("EVENT_QUEUE_URL", "https://sqs.eu-west-1.amazonaws.com/1/dapier-events")
    assert collect()["queue"]["name"] == "dapier-events"


def test_the_queue_carries_both_depth_spellings():
    """Relay's contract reads ``queue.pending``; this projection's brief named
    the same number ``depth``. Both ride along — null, like the age — so a
    console written against either spelling reads the same unknown rather
    than a missing key."""
    queue = collect()["queue"]
    assert {"pending", "depth", "oldest_pending_age_s"} <= set(queue)
    assert queue["pending"] is None
    assert queue["depth"] is None
    assert queue["oldest_pending_age_s"] is None


def test_the_carried_version_is_the_apps_own():
    payload = collect()
    assert payload["version"] == app_version == "0.1.0"
    assert payload["contract_version"] == 1
    assert payload["project"] == "dapier"
    assert payload["generated_at"] == "2026-10-08T15:30:00Z"


def test_run_history_is_not_claimed_as_empty_of_failures():
    """A zero here would read as "nothing failed"; the run log that would
    answer it belongs to Dapier's runs views, not to this projection."""
    payload = collect()
    assert payload["recent_runs"] == []
    assert payload["failures_24h"] is None


# --- the bearer gate --------------------------------------------------------------

def test_an_unset_token_answers_404(monkeypatch):
    monkeypatch.delenv("TASKDECK_STATUS_TOKEN", raising=False)
    status, body = taskdeck_status.api_status(request(), table_ref=Table())
    assert (status, body) == (404, {"error": "not found"})


def test_a_wrong_token_answers_the_same_404(monkeypatch):
    """A wrong token and a route that is not here are indistinguishable, so a
    caller learns nothing about whether this endpoint exists at all."""
    monkeypatch.setenv("TASKDECK_STATUS_TOKEN", "console-token")
    status, body = taskdeck_status.api_status(request(token="guess"), table_ref=Table())
    assert (status, body) == (404, {"error": "not found"})


def test_the_configured_token_returns_the_payload(monkeypatch):
    monkeypatch.setenv("TASKDECK_STATUS_TOKEN", "console-token")
    table = Table([fired("digest", NOW - timedelta(hours=1))])
    status, body = taskdeck_status.api_status(request(), table_ref=table, now=NOW)
    assert status == 200
    assert body["schedules"][0]["name"] == "digest"


def test_a_malformed_authorization_header_is_not_a_crash(monkeypatch):
    monkeypatch.setenv("TASKDECK_STATUS_TOKEN", "console-token")
    status, _ = taskdeck_status.api_status(
        request() | {"headers": {"Authorization": "Bearer"}}, table_ref=Table())
    assert status == 404


# --- through the router -----------------------------------------------------------

def test_the_route_is_served_outside_the_operator_gate(monkeypatch):
    """No session cookie, no operator sign-in: the bearer token is the whole
    credential, so this is not an /api/admin route."""
    monkeypatch.setenv("TASKDECK_STATUS_TOKEN", "console-token")
    response = router.handler(request(), None)
    assert response["statusCode"] == 200
    assert json.loads(response["body"])["project"] == "dapier"


def test_the_gateway_routes_it_and_allows_the_bearer_header():
    """Deploy-time wiring the router cannot see: an HTTP API 404s a path with
    no route event, and a browser preflight fails unless the stage's CORS
    configuration allows the Authorization header."""
    routes, allow_headers = _gateway_routes()
    assert "/internal/ops/status" in routes
    assert "authorization" in [header.lower() for header in allow_headers]


def test_other_methods_stay_not_found(monkeypatch):
    monkeypatch.setenv("TASKDECK_STATUS_TOKEN", "console-token")
    response = router.handler(
        {"requestContext": {"http": {"method": "POST",
                                     "path": "/internal/ops/status"}},
         "headers": {"Authorization": "Bearer console-token"}, "body": ""}, None)
    assert response["statusCode"] == 404
