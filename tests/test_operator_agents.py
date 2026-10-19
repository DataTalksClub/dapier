"""Operator agent orin@datatalks.club, and the read-only owner filter."""

import argparse
import json
import time
from pathlib import Path

import pytest

from src.dapier.api import runs
from src.dapier.api.admin import dispatch
from src.dapier.auth import actors, session, visibility
from src.dapier.host_tasks import api_list

ROOT = Path(__file__).resolve().parents[1]


def _token(monkeypatch, sub, subject):
    monkeypatch.setattr(session, "_credentials", lambda: {"password": "session-secret"})
    return session._sign({"sub": sub, "subject": subject, "exp": int(time.time()) + 3600})


def test_orin_is_an_agent_and_not_a_full_operator():
    payload = {"sub": "orin@datatalks.club", "subject": "sub-orin"}
    assert actors.is_agent(payload)
    assert actors.gate(payload) == "agent"
    assert actors.stable_subject(payload) == "sub-orin"
    assert actors.action_allowed(payload, "runs")
    assert actors.action_allowed(payload, "agent-tasks")
    assert actors.action_allowed(payload, "workflow.publish")
    assert actors.action_allowed(payload, "typefully.social_sets")
    assert not actors.action_allowed(payload, "credential")
    assert not actors.action_allowed(payload, "quota.set")
    assert actors.console_allowed("GET", "/api/admin/runs")
    assert actors.console_allowed("GET", "/api/admin/agent-tasks")
    assert actors.console_allowed("PUT", "/api/admin/designer/workflows")
    assert not actors.console_allowed("PUT", "/api/admin/credentials/slack")
    assert not actors.console_allowed("DELETE", "/api/admin/tokens")
    scope = visibility.for_session(payload)
    assert scope.is_operator is False
    assert scope.subject == "sub-orin"


def test_a_full_operator_is_not_narrowed_by_the_directory():
    payload = {"sub": "alexey@datatalks.club", "subject": "sub-alex"}
    assert actors.gate(payload) == "operator"
    assert visibility.for_session(payload).is_operator is True


def test_owner_filter_resolves_orin_and_rejects_unknown_names():
    match, error = actors.resolve_filter("orin")
    assert error is None
    assert "orin" in match
    assert "orin@datatalks.club" in match
    match, error = actors.resolve_filter(as_id="sub-9")
    assert match == {"sub-9"}
    match, error = actors.resolve_filter("orin", assignee="sub-9")
    assert match is None
    assert "same person" in error
    match, error = actors.resolve_filter("nobody")
    assert match is None
    assert error.startswith("Unknown owner")
    assert actors.row_visible(actors.resolve_filter("orin")[0], workflow_owner="ORIN@datatalks.club")
    assert not actors.row_visible(actors.resolve_filter("orin")[0], workflow_owner="sub-alex")


def test_runs_list_keeps_only_the_named_owner(monkeypatch):
    items = [
        {"run_id": "run-orin", "workflow_id": "wf-orin", "status": "completed",
         "started_at": "2026-10-01T00:00:00Z", "execution_id": "a"},
        {"run_id": "run-alex", "workflow_id": "wf-alex", "status": "completed",
         "started_at": "2026-10-02T00:00:00Z", "execution_id": "b"},
    ]
    monkeypatch.setattr(runs, "_scan_items", lambda filtered=False: (items, False))
    monkeypatch.setattr(visibility, "workflow_owners", lambda table_ref=None: {
        "wf-orin": "sub-orin", "wf-alex": "sub-alex",
    })

    class Table:
        def get_item(self, Key):
            if Key["pk"] == "actor#orin@datatalks.club":
                return {"Item": {"subject": "sub-orin"}}
            return {}

    monkeypatch.setattr(actors, "_bindings_table", lambda table_ref=None: Table())
    status, payload = runs.api_list(owner="orin")
    assert status == 200
    assert [row["run_id"] for row in payload["runs"]] == ["run-orin"]
    status, payload = runs.api_list(assignee="sub-alex")
    assert [row["run_id"] for row in payload["runs"]] == ["run-alex"]
    status, payload = runs.api_list(owner="nope")
    assert status == 400


def test_agent_tasks_list_filters_by_workflow_owner(monkeypatch):
    rows = [
        {"task_id": "agent:wf-orin:e1:a1", "kind": "agent", "status": "succeeded",
         "created_at": 2},
        {"task_id": "agent:wf-alex:e2:a1", "kind": "agent", "status": "succeeded",
         "created_at": 1},
    ]

    class Tasks:
        def scan(self, **kwargs):
            return {"Items": rows}

    monkeypatch.setattr(visibility, "workflow_owners", lambda table_ref=None: {
        "wf-orin": "orin@datatalks.club", "wf-alex": "alexey@datatalks.club",
    })
    status, payload = api_list(Tasks(), owner="orin")
    assert status == 200
    assert [task["task_id"] for task in payload["tasks"]] == ["agent:wf-orin:e1:a1"]


def test_me_offers_view_as_to_operators_and_narrows_orin(monkeypatch):
    operator = _token(monkeypatch, "alexey@datatalks.club", "sub-alex")
    event = {"headers": {"host": "dapier.example.test"},
             "cookies": [f"dapier_session={operator}"]}
    body = json.loads(dispatch._pre_routes(event, "GET", "/api/admin/me")["body"])
    assert body["role"] == "operator"
    assert body["view_as"][0]["alias"] == "orin"

    agent = _token(monkeypatch, "orin@datatalks.club", "sub-orin")
    event["cookies"] = [f"dapier_session={agent}"]
    body = json.loads(dispatch._pre_routes(event, "GET", "/api/admin/me")["body"])
    assert body["role"] == "agent"
    assert body["operator"] is True
    assert body["view_as"] == []


def test_orin_is_refused_on_an_operator_only_console_route(monkeypatch):
    token = _token(monkeypatch, "orin@datatalks.club", "sub-orin")
    event = {"headers": {"host": "dapier.example.test"},
             "cookies": [f"dapier_session={token}"]}
    response = dispatch.route(event, "GET", "/api/admin/tokens")
    assert response["statusCode"] == 403
    assert "role" in json.loads(response["body"])["error"]


def test_cli_accepts_owner_as_and_assignee():
    from dapier_cli.cli.ops import register as register_ops
    from dapier_cli.cli.runs import register as register_runs

    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="group")
    register_runs(sub)
    register_ops(sub)
    runs_args = parser.parse_args(["runs", "list", "--owner", "orin", "--as", "sub-1"])
    assert runs_args.owner == "orin" and runs_args.as_id == "sub-1"
    task_args = parser.parse_args(["agent-tasks", "list", "--assignee", "sub-1"])
    assert task_args.assignee == "sub-1"


def test_view_as_filters_runs_and_blocks_writes(monkeypatch):
    """A full operator picks Orin and the runs list asks for that owner.

    Write controls disappear, and a click on one that is forced visible
    does not reach the button.
    """
    pytest.importorskip("playwright.sync_api")
    from playwright.sync_api import sync_playwright

    from tests import test_console_boot as boot

    original = boot._api_payload

    def payload(path):
        if path == "/api/admin/me":
            return {
                "operator": True,
                "username": "alexey@datatalks.club",
                "name": "Alexey",
                "role": "operator",
                "view_as": [{
                    "alias": "orin",
                    "email": "orin@datatalks.club",
                    "label": "Orin",
                    "role": "agent",
                    "identities": ["orin", "orin@datatalks.club"],
                }],
            }
        if path.startswith("/api/admin/runs"):
            return {"runs": [], "paging": {}}
        return original(path)

    monkeypatch.setattr(boot, "_api_payload", payload)
    with boot.console_server() as origin, sync_playwright() as playwright:
        browser = boot._chromium(playwright)
        page = browser.new_page()
        try:
            page.goto(origin + "/runs", wait_until="domcontentloaded")
            page.locator("#app").wait_for(state="visible", timeout=10_000)
            page.locator("#view-as").wait_for(state="visible", timeout=10_000)
            with page.expect_request(
                lambda request: "/api/admin/runs?" in request.url and "owner=orin" in request.url,
                timeout=10_000,
            ):
                page.locator("#view-as").select_option("orin")
            assert page.evaluate("() => document.body.dataset.viewAs") == "orin"
            assert page.evaluate(
                "() => getComputedStyle(document.getElementById('home-new-workflow')).display"
            ) == "none"
            page.evaluate("""() => {
              const button = document.createElement('button');
              button.id = 'probe-replay';
              button.className = 'run-replay';
              button.textContent = 'Replay';
              document.body.appendChild(button);
              window.__viewAsWrite = false;
              button.addEventListener('click', () => { window.__viewAsWrite = true; });
              button.style.setProperty('display', 'inline-block', 'important');
            }""")
            page.locator("#probe-replay").click(force=True)
            assert page.evaluate("() => window.__viewAsWrite") is False
        finally:
            browser.close()


def test_console_view_as_is_wired_read_only():
    html = (ROOT / "src/web/index.html").read_text()
    assert 'id="view-as"' in html
    runs_js = (ROOT / "src/web/js/views/runs.js").read_text()
    agents_js = (ROOT / "src/web/js/views/agents.js").read_text()
    view_js = (ROOT / "src/web/js/view-as.js").read_text()
    assert "withOwner" in runs_js and "withOwner" in agents_js
    assert "data-view-as" in (ROOT / "src/web/app.css").read_text()
    assert "Read-only" in view_js or "read-only" in html
    assert "orin@datatalks.club" in (ROOT / "docs/operator-agents.md").read_text()
