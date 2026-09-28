"""Finding #8 — bulk enable/disable over several workflows in one call.

Domain (designer_store.api_bulk): each id goes through the same api_toggle
semantics and answers on its own — an unknown or invalid file fails that id
without stopping the batch. Both dispatchers expose
POST /api/{admin,agent}/designer/workflows/bulk, audited once as
``workflow.bulk-toggle`` with the id list; the CLI's
``workflows on|off|enable|disable a.yaml b.yaml`` loops through the same
endpoint, and the console's selection bar drives the admin route.
"""

import json

import pytest

from src.dapier.api import admin
from src.dapier.api import agent as agent_api
from src.dapier.api import designer_store
from src.dapier.auth import session
from src.dapier.triggers import published_workflows

WORKFLOW_YAML = """\
id: {id}
enabled: true
trigger:
  connector: email
  event: message.received
actions:
  - id: a1
    type: webhook
    url: https://example.test/{id}
"""

GITHUB_SCRIPT = {
    ("GET", "/repos/owner/repo/git/ref/heads/main"): {"object": {"sha": "base123"}},
    ("GET", "/repos/owner/repo/git/commits/base123"): {"tree": {"sha": "treesh"}},
    ("POST", "/repos/owner/repo/git/trees"): {"sha": "newtree"},
    ("POST", "/repos/owner/repo/git/commits"): {
        "sha": "commit456",
        "html_url": "https://github.com/owner/repo/commit/commit456",
    },
    ("PATCH", "/repos/owner/repo/git/refs/heads/main"): {},
}


class HistoryStubTable:
    def __init__(self):
        self.items = {}

    def put_item(self, Item):
        self.items[Item["workflow_id"]] = Item

    def get_item(self, Key):
        item = self.items.get(Key["workflow_id"])
        return {"Item": item} if item else {}

    def delete_item(self, Key):
        self.items.pop(Key["workflow_id"], None)

    def scan(self, **_):
        return {"Items": list(self.items.values())}


@pytest.fixture
def history_store(monkeypatch):
    monkeypatch.setenv(published_workflows.TABLE_ENV, "published-test")
    table = HistoryStubTable()
    monkeypatch.setattr(published_workflows, "get_table", lambda table_ref=None: table)
    return table


@pytest.fixture
def git_sync(monkeypatch):
    monkeypatch.setenv(designer_store.REPO_URL_ENV, "https://github.com/owner/repo")
    monkeypatch.delenv(designer_store.BRANCH_ENV, raising=False)
    monkeypatch.setattr(designer_store, "get_token", lambda: "test-token")
    monkeypatch.setattr(designer_store, "_github",
                        lambda method, path, token, payload=None:
                        GITHUB_SCRIPT[(method, path)])
    return monkeypatch


def seed_published(history_store, git_sync, *workflow_ids):
    for workflow_id in workflow_ids:
        status, _ = designer_store.api_save(
            {"yaml": WORKFLOW_YAML.format(id=workflow_id)}, operator="op-1", live=True)
        assert status == 200


# ---- Domain ----


def test_bulk_enable_and_disable_flip_each_published_item(git_sync, history_store):
    seed_published(history_store, git_sync, "flow-a", "flow-b")

    status, payload = designer_store.api_bulk(
        {"ids": ["flow-a.yaml", "flow-b.yaml"], "action": "disable"}, operator="op-2")

    assert status == 200
    assert payload["action"] == "disable"
    assert payload["requested"] == 2
    assert payload["ok"] == 2
    assert [result["id"] for result in payload["results"]] == ["flow-a.yaml", "flow-b.yaml"]
    assert all(result["ok"] for result in payload["results"])
    assert published_workflows.get_item("flow-a")["enabled"] is False
    assert published_workflows.get_item("flow-b")["enabled"] is False

    status, payload = designer_store.api_bulk(
        {"ids": ["flow-a.yaml"], "action": "enable"}, operator="op-2")
    assert status == 200
    assert published_workflows.get_item("flow-a")["enabled"] is True
    assert published_workflows.get_item("flow-b")["enabled"] is False


def test_bulk_fails_per_id_without_stopping_the_batch(git_sync, history_store):
    seed_published(history_store, git_sync, "flow-a")

    status, payload = designer_store.api_bulk(
        {"ids": ["flow-a.yaml", "ghost.yaml", "../escape.yaml"], "action": "enable"})

    assert status == 200
    results = {result["id"]: result for result in payload["results"]}
    assert results["flow-a.yaml"]["ok"] is True
    assert results["ghost.yaml"]["ok"] is False
    assert "no such workflow" in results["ghost.yaml"]["error"]
    assert results["../escape.yaml"]["ok"] is False
    assert "invalid workflow file name" in results["../escape.yaml"]["error"]
    assert payload["ok"] == 1
    # The healthy id still toggled.
    assert published_workflows.get_item("flow-a")["enabled"] is True


@pytest.mark.parametrize("body", [
    "not-an-object",
    {},
    {"ids": ["flow-a.yaml"]},
    {"action": "enable"},
    {"ids": ["flow-a.yaml"], "action": "delete"},
    {"ids": [], "action": "enable"},
    {"ids": "flow-a.yaml", "action": "enable"},
    {"ids": [""], "action": "enable"},
    {"ids": [42], "action": "enable"},
])
def test_bulk_rejects_malformed_bodies(git_sync, history_store, body):
    status, payload = designer_store.api_bulk(body)
    assert status == 400
    assert "error" in payload


def test_bulk_is_capped(git_sync, history_store):
    ids = [f"flow-{index}.yaml" for index in range(designer_store.MAX_BULK_IDS + 1)]
    status, payload = designer_store.api_bulk({"ids": ids, "action": "enable"})
    assert status == 400
    assert "at most" in payload["error"]


# ---- Console (admin) surface ----


def admin_request(method, path, body=None):
    return {
        "requestContext": {"http": {"method": method, "path": path}},
        "headers": {"host": "dapier.example.test"},
        "cookies": [],
        "body": json.dumps(body) if body is not None else None,
    }


@pytest.fixture
def operator_session(monkeypatch):
    monkeypatch.setattr(session, "authenticated", lambda event: True)
    monkeypatch.setattr(session, "_csrf_ok", lambda event, method: True)
    monkeypatch.setattr(session, "require_operator", lambda event: ({"sub": "op-1"}, None))
    # Roles v1: the dispatcher gates through require_role now.
    monkeypatch.setattr(session, "require_role",
                        lambda event, minimum="operator": ({"sub": "op-1"}, None))
    # Roles v1: the dispatcher gates through require_role now.
    monkeypatch.setattr(session, "require_role",
                        lambda event, minimum="operator": ({"sub": "op-1"}, None))
    monkeypatch.setattr(session, "_audit_event", lambda *args, **kwargs: None)


def test_admin_bulk_routes_to_the_store_and_audits_the_batch(monkeypatch, operator_session):
    seen = {}
    monkeypatch.setattr(designer_store, "api_bulk", lambda body, operator=None, visible=None:
                        seen.update(body=body, operator=operator)
                        or (200, {"action": "disable", "ok": 1, "results": []}))
    audits = []
    monkeypatch.setattr(session, "_audit_event",
                        lambda *args, **kwargs: audits.append((args, kwargs)) or None)

    body = {"ids": ["flow-a.yaml", "flow-b.yaml"], "action": "disable"}
    response = admin.route(
        admin_request("POST", "/api/admin/designer/workflows/bulk", body),
        "POST", "/api/admin/designer/workflows/bulk",
    )

    assert response["statusCode"] == 200
    assert seen == {"body": body, "operator": "op-1"}
    # ONE audit row for the whole batch, naming the id list.
    assert len(audits) == 1
    assert audits[0][0][0] == "flow-a.yaml, flow-b.yaml"
    assert audits[0][0][1] == "workflow.bulk-toggle"


def test_admin_bulk_requires_an_operator_session(monkeypatch):
    monkeypatch.setattr(session, "authenticated", lambda event: False)
    response = admin.route(
        admin_request("POST", "/api/admin/designer/workflows/bulk", {}),
        "POST", "/api/admin/designer/workflows/bulk",
    )
    assert response["statusCode"] == 401


# ---- CLI (agent) surface ----


def agent_request(method, path, body=None, token="dtc-token"):
    request = {
        "requestContext": {"http": {"method": method, "path": path}},
        "headers": {"host": "dapier.example.test",
                    "authorization": f"Bearer {token}"},
        "cookies": [],
    }
    if body is not None:
        request["body"] = json.dumps(body)
    return request


@pytest.fixture
def agent_identity(monkeypatch):
    monkeypatch.setenv("AUTH_CLI_CLIENT_ID", "cli-client")
    monkeypatch.delenv("OPERATOR_SUBJECTS", raising=False)
    monkeypatch.delenv("OPERATOR_EMAILS", raising=False)
    monkeypatch.setattr(
        agent_api, "verify_id_token", lambda token, audience=None: {"sub": "agent-op"},
    )
    emitted = []
    monkeypatch.setattr(agent_api, "audit", type("Audit", (), {
        "emit": staticmethod(lambda *args, **kwargs: emitted.append((args, kwargs))),
    }))
    return emitted


def test_agent_bulk_drives_the_same_store_and_audits_once(
        git_sync, history_store, agent_identity):
    seed_published(history_store, git_sync, "flow-a", "flow-b")

    body = {"ids": ["flow-a.yaml", "flow-b.yaml"], "action": "disable"}
    response = agent_api.route(
        agent_request("POST", "/api/agent/designer/workflows/bulk", body),
        "POST", "/api/agent/designer/workflows/bulk",
    )

    assert response["statusCode"] == 200
    payload = json.loads(response["body"])
    assert payload["ok"] == 2
    assert published_workflows.get_item("flow-a")["enabled"] is False
    assert published_workflows.get_item("flow-b")["enabled"] is False
    assert len(agent_identity) == 1
    assert agent_identity[0][0][1] == "workflow.bulk-toggle"
    assert agent_identity[0][0][2] == "agent-op"


# ---- CLI wrappers ----

from dapier_cli import commands as cli_commands
from dapier_cli import main as cli_main


def test_cli_enable_and_disable_accept_multiple_ids(monkeypatch, capsys):
    calls = []

    def fake_call(api_url, method, path, body=None, **kwargs):
        calls.append((method, path, body))
        return {"action": body["action"], "requested": 2, "ok": 2, "results": [
            {"id": "flow-a.yaml", "file": "flow-a.yaml", "ok": True,
             "enabled": body["action"] == "enable", "commit": "abc1234"},
            {"id": "flow-b.yaml", "file": "flow-b.yaml", "ok": True,
             "enabled": body["action"] == "enable", "commit": "abc1234"},
        ]}

    monkeypatch.setattr(cli_commands.api, "call", fake_call)

    assert cli_main.main(["workflows", "off", "flow-a.yaml", "flow-b.yaml"]) == 0
    assert calls[-1] == ("POST", "/api/agent/designer/workflows/bulk",
                         {"ids": ["flow-a.yaml", "flow-b.yaml"], "action": "disable"})
    out = capsys.readouterr().out
    assert out.count("is Off — live now.") == 2

    assert cli_main.main(["workflows", "enable", "flow-a.yaml", "flow-b.yaml"]) == 0
    assert calls[-1][2]["action"] == "enable"
    out = capsys.readouterr().out
    assert out.count("is On — live now.") == 2
    assert "abc1234" in out


def test_cli_toggle_reports_per_id_failures(monkeypatch, capsys):
    def fake_call(api_url, method, path, body=None, **kwargs):
        return {"action": "enable", "requested": 2, "ok": 1, "results": [
            {"id": "flow-a.yaml", "file": "flow-a.yaml", "ok": True,
             "enabled": True, "commit": "commit456"},
            {"id": "ghost.yaml", "ok": False,
             "error": "no such workflow: ghost.yaml"},
        ]}

    monkeypatch.setattr(cli_commands.api, "call", fake_call)
    assert cli_main.main(["workflows", "on", "flow-a.yaml", "ghost.yaml"]) == 0
    out = capsys.readouterr().out
    assert "flow-a.yaml is On" in out
    assert "ghost.yaml: no such workflow" in out
