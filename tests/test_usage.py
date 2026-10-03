"""Task usage metering, the monthly task quota, and their surfaces: the
worker rollup and gate, the usage/quota endpoints, the CLI, and the console
payload's quota block."""
import json
from datetime import datetime, timezone

import boto3
import pytest

from src.dapier.engine import usage as usage_rollup
from src.dapier.engine import worker


EVENT = {
    "id": "evt-1",
    "connector": "email",
    "event": "message.received",
    "correlation_id": "corr-1",
}

NOW = datetime(2026, 9, 15, tzinfo=timezone.utc)


class September2026(datetime):
    """Stands in for the module clock on paths without a ``now`` seam —
    the worker's internal gate and the API endpoints derive their month
    keys from ``datetime.now``."""

    @classmethod
    def now(cls, tz=None):
        return NOW


class FakeUsageTable:
    def __init__(self):
        self.updates = []
        self.by_month = {}
        self.items = {}
        self.fail_reads = False

    def update_item(self, **kwargs):
        self.updates.append(kwargs)
        key = kwargs["Key"]
        month = self.by_month.setdefault(key["month"], {})
        month[key["workflow_id"]] = month.get(key["workflow_id"], 0) + 1

    def get_item(self, **kwargs):
        if self.fail_reads:
            raise RuntimeError("dynamodb down")
        key = kwargs["Key"]
        item = self.items.get((key["month"], key["workflow_id"]))
        if item is None:
            tasks = self.by_month.get(key["month"], {}).get(key["workflow_id"])
            if tasks is None:
                return {}
            item = {"month": key["month"], "workflow_id": key["workflow_id"],
                    "tasks": tasks}
        return {"Item": item}

    def put_item(self, Item=None, **kwargs):
        Item = Item or kwargs.get("Item")
        self.items[(Item["month"], Item["workflow_id"])] = dict(Item)

    def delete_item(self, **kwargs):
        key = kwargs["Key"]
        self.items.pop((key["month"], key["workflow_id"]), None)

    def query(self, **kwargs):
        # boto3's Equals exposes the compared value only as ``_values``.
        wanted = kwargs["KeyConditionExpression"]._values[-1]
        items = [
            {"month": month, "workflow_id": workflow_id, "tasks": tasks}
            for month, workflows in self.by_month.items() if month == wanted
            for workflow_id, tasks in workflows.items()
        ]
        return {"Items": items}


@pytest.fixture()
def usage_table(monkeypatch):
    table = FakeUsageTable()
    monkeypatch.setenv("TASK_USAGE_TABLE", "task-usage")

    class Dynamo:
        def Table(self, _name):
            return table

    monkeypatch.setattr(boto3, "resource", lambda service: Dynamo())
    return table


def test_add_task_counts_one_completed_action_per_month(usage_table):
    usage_rollup.add_task("wf-1")

    assert len(usage_table.updates) == 2
    update = usage_table.updates[0]
    assert update["UpdateExpression"] == "ADD #tasks :one"
    assert update["ExpressionAttributeNames"] == {"#tasks": "tasks"}
    assert update["Key"]["workflow_id"] == "wf-1"
    # The second ADD is the account-wide row the quota gate reads.
    assert usage_table.updates[1]["Key"]["workflow_id"] == "_total"


def test_api_usage_returns_months_newest_first(usage_table):
    usage_table.by_month = {
        "202609": {"wf-1": 2, "wf-2": 1},
        "202608": {"wf-1": 1},
    }

    status, payload = usage_rollup.api_usage(
        months=2, now=datetime(2026, 9, 15, tzinfo=timezone.utc))
    assert status == 200
    rows = payload["usage"]
    assert [(row["month"], row["workflow_id"], row["tasks"]) for row in rows] == [
        ("202609", "wf-2", 1),
        ("202609", "wf-1", 2),
        ("202608", "wf-1", 1),
    ]


def test_api_usage_hides_the_account_total_row(usage_table):
    usage_table.by_month = {"202609": {"wf-1": 2, "_total": 3}}

    _, payload = usage_rollup.api_usage(
        months=1, now=datetime(2026, 9, 15, tzinfo=timezone.utc))
    assert payload["usage"] == [
        {"month": "202609", "workflow_id": "wf-1", "tasks": 2},
    ]


def test_api_usage_ignores_months_outside_the_window(usage_table):
    usage_table.by_month = {"202501": {"wf-1": 5}}

    _, payload = usage_rollup.api_usage(
        months=3, now=datetime(2026, 9, 15, tzinfo=timezone.utc))
    assert payload["usage"] == []


def _mark_with_usage(monkeypatch, usage_table, **kwargs):
    monkeypatch.setenv("EXECUTIONS_TABLE", "executions")

    class ExecTable:
        def update_item(self, **kwargs):
            pass

    class Dynamo:
        def Table(self, name):
            return usage_table if name == "task-usage" else ExecTable()

    monkeypatch.setattr(boto3, "resource", lambda service: Dynamo())
    worker._mark_completed("wf-1", "notify", EVENT, **kwargs)


def test_worker_completed_step_counts_as_a_task(usage_table, monkeypatch):
    _mark_with_usage(monkeypatch, usage_table)
    assert sorted(u["Key"]["workflow_id"] for u in usage_table.updates) == [
        "_total", "wf-1"]


def test_worker_filtered_step_is_not_a_task(usage_table, monkeypatch):
    _mark_with_usage(monkeypatch, usage_table, status="filtered")
    assert usage_table.updates == []


def test_worker_rollup_without_table_env_is_skipped(usage_table, monkeypatch):
    monkeypatch.delenv("TASK_USAGE_TABLE")
    _mark_with_usage(monkeypatch, usage_table)
    assert usage_table.updates == []


# --- the quota: store, read, enforce ---

def test_quota_is_uncapped_until_a_limit_is_set(usage_table):
    status, payload = usage_rollup.api_quota_get()

    assert status == 200
    assert payload["quota"]["enabled"] is False
    assert payload["quota"]["limit"] is None
    assert payload["quota"]["remaining"] is None


def test_quota_set_stores_the_budget_and_reports_the_standing(usage_table):
    usage_table.by_month = {"202609": {"_total": 7}}

    status, payload = usage_rollup.api_quota_set(100, now=NOW)

    assert status == 200
    quota = payload["quota"]
    assert quota["enabled"] is True
    assert quota["limit"] == 100
    assert quota["month"] == "202609"
    assert quota["used"] == 7
    assert quota["remaining"] == 93


def test_quota_set_accepts_a_numeric_string(usage_table):
    status, payload = usage_rollup.api_quota_set("250", now=NOW)

    assert status == 200
    assert payload["quota"]["limit"] == 250


def test_quota_off_clears_the_cap(usage_table):
    usage_rollup.api_quota_set(50)

    status, payload = usage_rollup.api_quota_set("off")

    assert status == 200
    assert payload["quota"]["enabled"] is False
    assert usage_table.items == {}


def test_quota_set_rejects_zero_and_garbage(usage_table):
    for bad in (0, -3, "many", True, [100]):
        status, payload = usage_rollup.api_quota_set(bad)
        assert status == 400, bad
        assert "error" in payload
    assert usage_table.items == {}


def test_quota_set_without_a_usage_table_still_answers(usage_table, monkeypatch):
    monkeypatch.delenv("TASK_USAGE_TABLE")

    status, payload = usage_rollup.api_quota_set(10)

    assert status == 200
    assert payload["quota"]["enabled"] is False


def test_enforce_raises_once_the_budget_is_spent(usage_table):
    usage_rollup.api_quota_set(5)
    usage_table.by_month = {"202609": {"_total": 5}}

    with pytest.raises(usage_rollup.QuotaExceeded) as excinfo:
        usage_rollup.enforce("wf-1", now=NOW)

    message = str(excinfo.value)
    assert "202609" in message
    assert "5/5" in message
    assert "dapier quota set" in message


def test_enforce_allows_the_step_while_budget_remains(usage_table):
    usage_rollup.api_quota_set(5)
    usage_table.by_month = {"202609": {"_total": 4}}

    assert usage_rollup.enforce("wf-1", now=NOW) is None


def test_enforce_is_silent_without_a_usage_table(usage_table, monkeypatch):
    monkeypatch.delenv("TASK_USAGE_TABLE")

    assert usage_rollup.enforce("wf-1", now=NOW) is None


def test_enforce_opens_the_gate_when_the_read_fails(usage_table):
    # Usage must never break a run: an infra failure on the quota read
    # lets the step through rather than failing it.
    usage_rollup.api_quota_set(5)
    usage_table.fail_reads = True

    assert usage_rollup.enforce("wf-1", now=NOW) is None


# --- the worker gate ---

def test_worker_gate_fails_the_step_when_the_budget_is_spent(usage_table, monkeypatch):
    monkeypatch.setattr(usage_rollup, "datetime", September2026)
    usage_rollup.api_quota_set(2)
    usage_table.by_month = {"202609": {"_total": 2}}

    hooks = worker._attempt_hooks(0)
    with pytest.raises(usage_rollup.QuotaExceeded):
        hooks["before_action"]("wf-1", "notify", EVENT, "slack")


def test_worker_gate_delegates_when_budget_remains(usage_table, monkeypatch):
    usage_rollup.api_quota_set(10)
    usage_table.by_month = {"202609": {"_total": 2}}
    monkeypatch.setattr(worker, "_is_pending",
                        lambda *args, **kwargs: True)

    hooks = worker._attempt_hooks(0)
    assert hooks["before_action"]("wf-1", "notify", EVENT, "slack") is True


def test_resume_path_gates_through_the_same_hook(usage_table, monkeypatch):
    monkeypatch.setattr(usage_rollup, "datetime", September2026)
    usage_rollup.api_quota_set(2)
    usage_table.by_month = {"202609": {"_total": 2}}

    with pytest.raises(usage_rollup.QuotaExceeded):
        worker._quota_pending("wf-1", "notify", EVENT, "slack")


# --- the engine routes the gate through the action-error machinery ---

def _run_gate_chain(monkeypatch, step, before_action):
    from src.dapier.engine import logic
    from src.dapier.engine.logic_pkg import execution

    ran, seen = [], {"after": [], "error": []}
    monkeypatch.setattr(execution, "_execute_step",
                        lambda *args, **kwargs: pytest.fail("must not run"))
    outcome = {}
    try:
        outcome["returned"] = logic.run_chain(
            "wf-1", [step], EVENT, lambda *args, **kwargs: ran.append(args),
            before_action=before_action,
            after_action=lambda wid, aid, event, **kw: seen["after"].append(kw),
            on_action_error=lambda wid, aid, event, exc, **kw: seen["error"].append(exc),
        )
    except Exception as exc:  # noqa: BLE001 — the test asserts on what rose
        outcome["raised"] = exc
    return ran, seen, outcome


def test_quota_gate_failure_takes_the_action_error_path(usage_table, monkeypatch):
    from src.dapier.engine import logic

    usage_rollup.api_quota_set(1)
    usage_table.by_month = {"202609": {"_total": 1}}

    def before_action(wid, aid, event, action_type=None):
        usage_rollup.enforce(wid, now=NOW)
        return True

    ran, seen, outcome = _run_gate_chain(monkeypatch, {"type": "slack"},
                                         before_action)

    # halt (the default): the step record closes failed via the error hook
    # and the chain re-raises — the run is a loud failure, not a silent skip.
    assert ran == []
    assert len(seen["error"]) == 1
    assert isinstance(seen["error"][0], logic.QuotaExceeded)
    assert seen["after"] == []
    assert isinstance(outcome.get("raised"), logic.QuotaExceeded)


def test_quota_gate_is_absorbed_by_on_fail_continue(usage_table, monkeypatch):
    usage_rollup.api_quota_set(1)
    usage_table.by_month = {"202609": {"_total": 1}}

    def before_action(wid, aid, event, action_type=None):
        usage_rollup.enforce(wid, now=NOW)
        return True

    ran, seen, outcome = _run_gate_chain(
        monkeypatch, {"type": "slack", "on_fail": "continue"}, before_action)

    assert ran == []
    assert len(seen["error"]) == 1
    assert [entry["status"] for entry in seen["after"]] == ["skipped"]
    assert "raised" not in outcome
    assert outcome["returned"] is None


def test_a_lease_busy_gate_is_not_an_action_failure(usage_table, monkeypatch):
    from src.dapier.engine.worker import LeaseBusy

    def before_action(wid, aid, event, action_type=None):
        raise LeaseBusy("a delivery is still in flight")

    ran, seen, outcome = _run_gate_chain(monkeypatch, {"type": "slack"},
                                         before_action)

    # The quiet requeue: LeaseBusy keeps its own semantics — no error hook,
    # no error policy, the exception passes untouched.
    assert ran == []
    assert seen["error"] == []
    assert isinstance(outcome.get("raised"), LeaseBusy)


# --- the surfaces ---

def test_admin_quota_endpoints_get_and_save(usage_table, monkeypatch):
    from src.dapier.api.admin import routes

    audited = []
    monkeypatch.setattr(routes.session, "_audit_event",
                        lambda *args, **kwargs: audited.append(args))

    response = routes.quota_get({})
    assert response["statusCode"] == 200
    assert json.loads(response["body"])["quota"]["enabled"] is False

    response = routes.quota_save({"body": json.dumps({"limit": 250})}, "op-1")
    assert response["statusCode"] == 200
    assert json.loads(response["body"])["quota"]["limit"] == 250
    assert audited, "the settings write must land in the audit trail"

    response = routes.quota_save({"body": json.dumps({"limit": 0})}, "op-1")
    assert response["statusCode"] == 400


def test_admin_quota_save_rejects_a_broken_body(usage_table):
    from src.dapier.api.admin import routes

    assert routes.quota_save({"body": "not json"}, "op-1")["statusCode"] == 400
    assert routes.quota_save({"body": json.dumps([1])}, "op-1")["statusCode"] == 400


def test_agent_quota_get_and_put_serve_the_same_domain(usage_table, monkeypatch):
    from src.dapier.api import agent as agent_api

    seen_actions = []

    def fake_operator(event, action):
        seen_actions.append(action)
        return None, None

    monkeypatch.setattr(agent_api, "require_operator", fake_operator)
    emitted = []
    monkeypatch.setattr(agent_api.audit, "emit",
                        lambda *args, **kwargs: emitted.append(args))

    response = agent_api.route({"headers": {}}, "GET", "/api/agent/quota")
    assert response["statusCode"] == 200
    assert json.loads(response["body"])["quota"]["enabled"] is False

    response = agent_api.route(
        {"headers": {}, "body": json.dumps({"limit": "40"})},
        "PUT", "/api/agent/quota")
    assert response["statusCode"] == 200
    assert json.loads(response["body"])["quota"]["limit"] == 40
    assert "quota.set" in seen_actions
    assert emitted, "the agent-side set must be audited too"

    response = agent_api.route(
        {"headers": {}, "body": json.dumps({"limit": 0})},
        "PUT", "/api/agent/quota")
    assert response["statusCode"] == 400


def test_agent_quota_put_requires_an_operator(usage_table):
    from src.dapier.api import agent as agent_api

    response = agent_api.route(
        {"headers": {}, "body": json.dumps({"limit": 40})},
        "PUT", "/api/agent/quota")

    assert response["statusCode"] == 401


def test_agent_usage_payload_carries_the_quota_block(usage_table, monkeypatch):
    from src.dapier.api import agent as agent_api

    monkeypatch.setattr(usage_rollup, "datetime", September2026)
    usage_table.by_month = {"202609": {"wf-2": 5}}
    monkeypatch.setattr(agent_api, "require_operator", lambda event, action: (None, None))

    response = agent_api.route(
        {"headers": {}, "queryStringParameters": {"months": "1"}},
        "GET", "/api/agent/usage",
    )

    body = json.loads(response["body"])
    assert body["usage"] == [{"month": "202609", "workflow_id": "wf-2", "tasks": 5}]
    assert body["quota"]["enabled"] is False


def test_overview_quota_block(usage_table, monkeypatch):
    from src.dapier.api import overview

    assert overview._quota()["enabled"] is False
    monkeypatch.delenv("TASK_USAGE_TABLE")
    assert overview._quota() is None


def test_cli_parses_quota_show_and_set():
    from dapier_cli import main as cli

    parser = cli.build_parser()
    assert parser.parse_args(["quota"]).group == "quota"
    assert parser.parse_args(["quota"]).command is None
    assert parser.parse_args(["quota", "set", "100"]).limit == "100"
    assert parser.parse_args(["quota", "set", "off"]).limit == "off"


def test_cli_quota_set_sends_the_limit(usage_table, monkeypatch, capsys):
    from dapier_cli import api as cli_api, commands

    calls = []

    def fake_call(url, method, path, body=None, debug=False):
        calls.append((method, path, body))
        return {"quota": {"enabled": True, "limit": 100, "used": 3,
                          "remaining": 97, "month": "202609"}}

    monkeypatch.setattr(cli_api, "call", fake_call)

    assert commands.quota("http://x", False, command="set", limit="100") == 0
    assert calls == [("PUT", "/api/agent/quota", {"limit": "100"})]
    assert "3/100" in capsys.readouterr().out

    assert commands.quota("http://x", False) == 0
    assert calls[-1] == ("GET", "/api/agent/quota", None)


def test_cli_usage_prints_the_quota_line(usage_table, monkeypatch, capsys):
    from dapier_cli import api as cli_api, commands

    monkeypatch.setattr(cli_api, "call", lambda *args, **kwargs: {
        "usage": [],
        "quota": {"enabled": True, "limit": 50, "used": 12,
                  "remaining": 38, "month": "202609"},
    })

    assert commands.usage("http://x", False) == 0
    out = capsys.readouterr().out
    assert "12/50" in out
    assert "38 left" in out


def test_cli_parses_usage_months():
    from dapier_cli import main as cli

    assert cli.build_parser().parse_args(["usage"]).months == 12
    assert cli.build_parser().parse_args(["usage", "--months", "3"]).months == 3


def test_cli_parses_errors_days():
    from dapier_cli import main as cli

    assert cli.build_parser().parse_args(["errors"]).days == 7
    assert cli.build_parser().parse_args(["errors", "--days", "14"]).days == 14


if __name__ == "__main__":
    pytest.main([__file__])
