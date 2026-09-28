"""Server-side run-history filtering and pagination: the shared domain list,
both operator routes (agent + admin), and the CLI flag plumbing."""
import json
import time
from datetime import datetime, timedelta, timezone

import boto3
import pytest

from dapier_cli import commands, main
from src.dapier.api import admin
from src.dapier.api import agent as agent_api
from src.dapier.api import runs
from src.dapier.auth import session


@pytest.fixture
def isolated_home(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.delenv("DAPIER_API_URL", raising=False)
    return tmp_path


class FakeExecTable:
    """One-shot scan of the whole window (DynamoDB pages via the loop cap)."""

    def __init__(self, items):
        self.items = items

    def scan(self, **kwargs):
        return {"Items": list(self.items)}

    def query(self, **kwargs):
        values = list((kwargs.get("ExpressionAttributeValues") or {}).values())
        wanted = values[0] if values else None
        return {"Items": [item for item in self.items if item.get("run_id") == wanted]}


class PagedExecTable:
    """Scan hands out fixed-size pages with LastEvaluatedKey, like DynamoDB."""

    def __init__(self, items, page_size):
        self.items = items
        self.page_size = page_size

    def scan(self, **kwargs):
        start = (kwargs.get("ExclusiveStartKey") or {}).get("index", 0)
        end = start + self.page_size
        response = {"Items": self.items[start:end]}
        if end < len(self.items):
            response["LastEvaluatedKey"] = {"index": end}
        return response


def _configure(monkeypatch, items):
    monkeypatch.setenv("EXECUTIONS_TABLE", "executions")

    class Dynamo:
        def Table(self, _name):
            return FakeExecTable(items)

    monkeypatch.setattr(boto3, "resource", lambda service: Dynamo())


def _run(workflow_id, event_id, *, status="completed", started="2026-09-25T10:00:00+00:00"):
    return {
        "execution_id": f"{workflow_id}:post:{event_id}",
        "run_id": f"{workflow_id}:{event_id}",
        "workflow_id": workflow_id,
        "action_id": "post",
        "action_type": "webhook",
        "connector": "email",
        "event_type": "message.received",
        "status": status,
        "started_at": started,
        "finished_at": started,
    }


def _spread(count, *, base=None):
    """`count` distinct ascending timestamps an hour apart (from 20:00)."""
    base = base or datetime(2026, 9, 20, 20, tzinfo=timezone.utc)
    return [(base + timedelta(hours=i)).isoformat() for i in range(count)]


# --- domain: filters -------------------------------------------------------


def test_api_list_filters_by_before_exclusively(monkeypatch):
    stamps = _spread(3)  # 20:00, 21:00, 22:00 on 2026-09-20
    _configure(monkeypatch, [
        *[_run("wf-1", f"evt-{i}", started=stamp) for i, stamp in enumerate(stamps)],
        _run("wf-1", "evt-next-day", started="2026-09-21T09:00:00+00:00"),
    ])

    _, payload = runs.api_list(before="2026-09-20T21:00:00+00:00")
    assert [run["run_id"] for run in payload["runs"]] == ["wf-1:evt-0"]

    _, payload = runs.api_list(before="2026-09-21")  # date-only: all of the 20th, not the 21st
    assert [run["run_id"] for run in payload["runs"]] == ["wf-1:evt-2", "wf-1:evt-1", "wf-1:evt-0"]


def test_api_list_window_is_since_inclusive_before_exclusive(monkeypatch):
    stamps = _spread(3)
    _configure(monkeypatch, [_run("wf-1", f"evt-{i}", started=stamp)
                             for i, stamp in enumerate(stamps)])

    _, payload = runs.api_list(since="2026-09-20T21:00:00+00:00",
                               before="2026-09-20T22:00:00+00:00")
    assert [run["run_id"] for run in payload["runs"]] == ["wf-1:evt-1"]


def test_status_aliases_success_and_problems(monkeypatch):
    _configure(monkeypatch, [
        _run("wf-1", "evt-ok", status="completed"),
        _run("wf-1", "evt-gated", status="filtered"),
        _run("wf-2", "evt-boom", status="failed"),
        _run("wf-2", "evt-live", status="processing"),
    ])

    _, payload = runs.api_list(status="success")
    assert sorted(run["run_id"] for run in payload["runs"]) == ["wf-1:evt-gated", "wf-1:evt-ok"]

    _, payload = runs.api_list(status="problems")
    assert [run["run_id"] for run in payload["runs"]] == ["wf-2:evt-boom"]

    _, payload = runs.api_list(status="processing")  # exact status still passes through
    assert [run["run_id"] for run in payload["runs"]] == ["wf-2:evt-live"]


# --- domain: limit and paging ---------------------------------------------


def test_limit_defaults_to_25_and_caps_at_200(monkeypatch):
    stamps = _spread(300, base=datetime(2026, 8, 1, tzinfo=timezone.utc))
    _configure(monkeypatch, [_run("wf-1", f"evt-{i}", started=stamp)
                             for i, stamp in enumerate(stamps)])

    _, payload = runs.api_list()
    assert payload["paging"]["limit"] == 25
    assert payload["paging"]["filtered"] is False
    assert payload["paging"]["next"]  # 300 runs: the default page is full
    assert len(payload["runs"]) == 25

    _, payload = runs.api_list(limit="10000")
    assert payload["paging"]["limit"] == 200
    assert len(payload["runs"]) == 200
    assert payload["paging"]["filtered"] is False


def test_next_token_pages_strictly_after_without_overlap(monkeypatch):
    stamps = _spread(5)
    _configure(monkeypatch, [_run("wf-1", f"evt-{i}", started=stamp)
                             for i, stamp in enumerate(stamps)])

    _, page1 = runs.api_list(limit=2)
    assert [run["run_id"] for run in page1["runs"]] == ["wf-1:evt-4", "wf-1:evt-3"]
    assert page1["paging"]["next"]

    _, page2 = runs.api_list(limit=2, next_token=page1["paging"]["next"])
    assert [run["run_id"] for run in page2["runs"]] == ["wf-1:evt-2", "wf-1:evt-1"]
    assert page2["paging"]["next"]

    _, page3 = runs.api_list(limit=2, next_token=page2["paging"]["next"])
    assert [run["run_id"] for run in page3["runs"]] == ["wf-1:evt-0"]
    assert page3["paging"]["next"] is None

    seen = [run["run_id"] for page in (page1, page2, page3) for run in page["runs"]]
    assert len(seen) == len(set(seen)) == 5


def test_next_token_tie_breaks_on_run_id(monkeypatch):
    started = "2026-09-25T10:00:00+00:00"
    _configure(monkeypatch, [_run("wf-1", "evt-a", started=started),
                             _run("wf-1", "evt-b", started=started)])

    _, page1 = runs.api_list(limit=1)
    assert [run["run_id"] for run in page1["runs"]] == ["wf-1:evt-b"]

    _, page2 = runs.api_list(limit=1, next_token=page1["paging"]["next"])
    assert [run["run_id"] for run in page2["runs"]] == ["wf-1:evt-a"]
    assert page2["paging"]["next"] is None


def test_paging_composes_with_filters(monkeypatch):
    stamps = _spread(4)
    _configure(monkeypatch, [
        _run("wf-1", "evt-0", status="failed", started=stamps[0]),
        _run("wf-2", "evt-1", started=stamps[1]),
        _run("wf-1", "evt-2", status="failed", started=stamps[2]),
        _run("wf-1", "evt-3", started=stamps[3]),
    ])

    _, page1 = runs.api_list(workflow_id="wf-1", status="failed", limit=1)
    assert [run["run_id"] for run in page1["runs"]] == ["wf-1:evt-2"]
    assert page1["paging"]["filtered"] is True

    _, page2 = runs.api_list(workflow_id="wf-1", status="failed", limit=1,
                             next_token=page1["paging"]["next"])
    assert [run["run_id"] for run in page2["runs"]] == ["wf-1:evt-0"]
    assert page2["paging"]["next"] is None


def test_invalid_next_token_is_400(monkeypatch):
    _configure(monkeypatch, [_run("wf-1", "evt-1")])
    status, payload = runs.api_list(next_token="not-a-token")
    assert status == 400
    assert "token" in payload["error"]


def test_scan_walks_pages_until_the_window_ends(monkeypatch):
    stamps = _spread(250, base=datetime(2026, 8, 1, tzinfo=timezone.utc))
    items = [_run("wf-1", f"evt-{i}", started=stamp) for i, stamp in enumerate(stamps)]
    monkeypatch.setattr(runs, "_table", lambda: PagedExecTable(items, page_size=100))

    _, payload = runs.api_list(limit=25)

    assert len(payload["runs"]) == 25  # the walk crossed three pages
    assert payload["runs"][0]["run_id"] == "wf-1:evt-249"


def test_recent_stays_a_plain_backward_compatible_list(monkeypatch):
    _configure(monkeypatch, [_run("wf-1", "evt-1"), _run("wf-1", "evt-2")])
    listing = runs.recent(1)
    assert isinstance(listing, list) and len(listing) == 1
    assert listing[0]["run_id"] == "wf-1:evt-2"


# --- agent route -----------------------------------------------------------


def _configure_agent(monkeypatch, items):
    agent_api.reset_rate_limits()
    monkeypatch.setenv("AUTH_CLI_CLIENT_ID", "cli-client")
    monkeypatch.setenv("OPERATOR_EMAILS", "op@datatalks.club")
    monkeypatch.delenv("AUDIT_TABLE", raising=False)
    monkeypatch.setattr(agent_api, "verify_id_token",
                        lambda token, audience=None: {"sub": "op-1", "email": "op@datatalks.club"})
    monkeypatch.setattr(agent_api.runs, "_table", lambda: FakeExecTable(items))


def _bearer_event(query=None):
    return {
        "headers": {"host": "dapier.example.test", "authorization": "Bearer dtc-token"},
        "cookies": [],
        "queryStringParameters": query,
    }


def test_agent_runs_route_filters_and_pages(monkeypatch):
    stamps = _spread(3)
    _configure_agent(monkeypatch, [
        _run("wf-1", "evt-0", status="failed", started=stamps[0]),
        _run("wf-2", "evt-1", status="failed", started=stamps[1]),
        _run("wf-1", "evt-2", status="failed", started=stamps[2]),
    ])

    listed = agent_api.route(
        _bearer_event({"workflow": "wf-1", "status": "problems", "limit": "1"}),
        "GET", "/api/agent/runs",
    )

    assert listed["statusCode"] == 200
    body = json.loads(listed["body"])
    assert [run["run_id"] for run in body["runs"]] == ["wf-1:evt-2"]
    assert body["paging"]["next"]
    assert body["paging"]["filtered"] is True

    paged = agent_api.route(
        _bearer_event({"workflow": "wf-1", "status": "problems", "limit": "1",
                       "next": body["paging"]["next"]}),
        "GET", "/api/agent/runs",
    )
    assert paged["statusCode"] == 200
    assert [run["run_id"] for run in json.loads(paged["body"])["runs"]] == ["wf-1:evt-0"]


def test_agent_runs_route_still_requires_operator(monkeypatch):
    _configure_agent(monkeypatch, [_run("wf-1", "evt-1")])
    monkeypatch.setenv("OPERATOR_EMAILS", "someone-else@datatalks.club")

    listed = agent_api.route(_bearer_event({"before": "2026-09-26"}), "GET", "/api/agent/runs")

    assert listed["statusCode"] == 403


def test_agent_runs_route_forwards_content_search(monkeypatch):
    stamps = _spread(2)
    _configure_agent(monkeypatch, [
        _run("wf-1", "evt-0", started=stamps[0]),
        _run("wf-2", "evt-1", started=stamps[1]),
    ])

    listed = agent_api.route(
        _bearer_event({"q": "wf-2:evt-1"}),
        "GET", "/api/agent/runs",
    )

    assert listed["statusCode"] == 200
    body = json.loads(listed["body"])
    assert [run["run_id"] for run in body["runs"]] == ["wf-2:evt-1"]
    assert body["paging"]["filtered"] is True


# --- admin route -----------------------------------------------------------


def _configure_admin(monkeypatch, items):
    monkeypatch.setattr(session, "_credentials", lambda: {"username": "admin", "password": "pw"})
    monkeypatch.setenv("OPERATOR_EMAILS", "op@datatalks.club")
    cookie = session._sign({"sub": "op@datatalks.club", "subject": "op-sub",
                            "exp": int(time.time()) + 600})

    class Dynamo:
        def Table(self, _name):
            return FakeExecTable(items)

    monkeypatch.setenv("EXECUTIONS_TABLE", "executions")
    monkeypatch.setattr(boto3, "resource", lambda service: Dynamo())
    return [f"dapier_session={cookie}"]


def _admin_request(method, path, query=None, cookies=None):
    return {
        "requestContext": {"http": {"method": method, "path": path}},
        "headers": {"host": "dapier.example.test", "origin": "https://dapier.example.test"},
        "cookies": cookies or [],
        "queryStringParameters": query,
        "body": None,
    }


def test_admin_runs_route_filters_and_pages(monkeypatch):
    stamps = _spread(3)
    cookies = _configure_admin(monkeypatch, [
        _run("wf-1", "evt-0", started=stamps[0]),
        _run("wf-2", "evt-1", started=stamps[1]),
        _run("wf-2", "evt-2", started=stamps[2]),
    ])

    listed = admin.route(
        _admin_request("GET", "/api/admin/runs",
                       query={"workflow_id": "wf-2", "limit": "1"}, cookies=cookies),
        "GET", "/api/admin/runs",
    )

    assert listed["statusCode"] == 200
    body = json.loads(listed["body"])
    assert [run["run_id"] for run in body["runs"]] == ["wf-2:evt-2"]
    assert body["paging"]["next"]

    paged = admin.route(
        _admin_request("GET", "/api/admin/runs",
                       query={"workflow_id": "wf-2", "limit": "1",
                              "next": body["paging"]["next"]}, cookies=cookies),
        "GET", "/api/admin/runs",
    )
    assert paged["statusCode"] == 200
    assert [run["run_id"] for run in json.loads(paged["body"])["runs"]] == ["wf-2:evt-1"]


def test_admin_runs_route_rejects_a_bad_token(monkeypatch):
    cookies = _configure_admin(monkeypatch, [_run("wf-1", "evt-1")])

    listed = admin.route(
        _admin_request("GET", "/api/admin/runs", query={"next": "junk"}, cookies=cookies),
        "GET", "/api/admin/runs",
    )

    assert listed["statusCode"] == 400


def test_admin_runs_route_forwards_content_search(monkeypatch):
    stamps = _spread(2)
    cookies = _configure_admin(monkeypatch, [
        _run("wf-1", "evt-0", started=stamps[0]),
        _run("wf-2", "evt-1", started=stamps[1]),
    ])

    listed = admin.route(
        _admin_request("GET", "/api/admin/runs", query={"q": "wf-1:evt-0"}, cookies=cookies),
        "GET", "/api/admin/runs",
    )

    assert listed["statusCode"] == 200
    body = json.loads(listed["body"])
    assert [run["run_id"] for run in body["runs"]] == ["wf-1:evt-0"]


# --- CLI -------------------------------------------------------------------


def _stub_api(monkeypatch, response):
    calls = []

    def fake_call(api_url, method, path, body=None, **kwargs):
        calls.append((method, path))
        return response

    monkeypatch.setattr(commands.api, "call", fake_call)
    return calls


def test_cli_forwards_all_filter_flags(isolated_home, monkeypatch):
    calls = _stub_api(monkeypatch, {"runs": []})

    rc = main.main(["runs", "list", "--workflow", "wf-1", "--status", "problems",
                    "--since", "2026-09-01", "--before", "2026-09-30",
                    "--search", "order-1234", "--limit", "5"])

    assert rc == 0
    assert calls == [("GET", "/api/agent/runs?limit=5&workflow_id=wf-1&status=problems"
                              "&since=2026-09-01&before=2026-09-30&q=order-1234")]


def test_cli_prints_next_page_footer_only_when_paging(isolated_home, monkeypatch, capsys):
    calls = _stub_api(monkeypatch, {
        "runs": [{"run_id": "wf-1:evt-1", "workflow_id": "wf-1", "status": "failed",
                  "steps": 1, "started_at": "2026-09-25T10:00:00+00:00"}],
        "paging": {"next": "tok-2", "limit": 25, "filtered": True},
    })

    rc = main.main(["runs", "list"])

    assert rc == 0
    assert calls == [("GET", "/api/agent/runs?limit=25")]
    out = capsys.readouterr().out
    assert "next page: tok-2" in out


def test_cli_has_no_footer_without_a_next_page(isolated_home, monkeypatch, capsys):
    _stub_api(monkeypatch, {
        "runs": [{"run_id": "wf-1:evt-1", "workflow_id": "wf-1", "status": "failed",
                  "steps": 1, "started_at": "2026-09-25T10:00:00+00:00"}],
        "paging": {"next": None, "limit": 25, "filtered": False},
    })

    rc = main.main(["runs", "list"])

    assert rc == 0
    assert "next page:" not in capsys.readouterr().out


def test_cli_passes_next_token_back(isolated_home, monkeypatch, capsys):
    calls = _stub_api(monkeypatch, {
        "runs": [{"run_id": "wf-1:evt-0", "workflow_id": "wf-1", "status": "failed",
                  "steps": 1, "started_at": "2026-09-25T09:00:00+00:00"}],
        "paging": {"next": "tok-3", "limit": 25, "filtered": True},
    })

    rc = main.main(["runs", "list", "--next", "tok-2"])

    assert rc == 0
    assert calls == [("GET", "/api/agent/runs?limit=25&next=tok-2")]
    assert "next page: tok-3" in capsys.readouterr().out


if __name__ == "__main__":
    pytest.main([__file__])
