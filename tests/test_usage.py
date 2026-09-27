"""Task usage metering: the worker rollup, the usage endpoints, and the CLI."""
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


class FakeUsageTable:
    def __init__(self):
        self.updates = []
        self.by_month = {}

    def update_item(self, **kwargs):
        self.updates.append(kwargs)
        key = kwargs["Key"]
        month = self.by_month.setdefault(key["month"], {})
        month[key["workflow_id"]] = month.get(key["workflow_id"], 0) + 1

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

    assert len(usage_table.updates) == 1
    update = usage_table.updates[0]
    assert update["UpdateExpression"] == "ADD #tasks :one"
    assert update["ExpressionAttributeNames"] == {"#tasks": "tasks"}
    assert update["Key"]["workflow_id"] == "wf-1"


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
    assert len(usage_table.updates) == 1


def test_worker_filtered_step_is_not_a_task(usage_table, monkeypatch):
    _mark_with_usage(monkeypatch, usage_table, status="filtered")
    assert usage_table.updates == []


def test_worker_rollup_without_table_env_is_skipped(usage_table, monkeypatch):
    monkeypatch.delenv("TASK_USAGE_TABLE")
    _mark_with_usage(monkeypatch, usage_table)
    assert usage_table.updates == []


def test_admin_usage_endpoint_serves_the_rollup(usage_table):
    from src.dapier.api.admin import routes

    usage_table.by_month = {"202609": {"wf-1": 3}, "202608": {"wf-1": 1}}

    response = routes.usage({"queryStringParameters": {"months": "2"}})

    assert response["statusCode"] == 200
    body = json.loads(response["body"])
    assert [(row["month"], row["workflow_id"], row["tasks"]) for row in body["usage"]] == [
        ("202609", "wf-1", 3),
        ("202608", "wf-1", 1),
    ]


def test_agent_usage_endpoint_serves_the_same_domain_function(usage_table, monkeypatch):
    from src.dapier.api import agent as agent_api

    usage_table.by_month = {"202609": {"wf-2": 5}}
    monkeypatch.setattr(agent_api, "require_operator", lambda event, action: (None, None))

    response = agent_api.route(
        {"headers": {}, "queryStringParameters": {"months": "1"}},
        "GET", "/api/agent/usage",
    )

    assert response["statusCode"] == 200
    body = json.loads(response["body"])
    assert body["usage"] == [{"month": "202609", "workflow_id": "wf-2", "tasks": 5}]


def test_agent_usage_endpoint_requires_an_operator(usage_table, monkeypatch):
    from src.dapier.api import agent as agent_api

    # No authorization header: the operator gate answers before any read.
    response = agent_api.route({"headers": {}}, "GET", "/api/agent/usage")

    assert response["statusCode"] == 401


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
