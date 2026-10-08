"""Workflow folders on every surface: the designer store, the admin and
agent routes behind the console and CLI, and the CLI commands themselves.

Zapier folders are flat: a workflow sits in at most one folder (or none),
and a folder is a name, never a path. The folder is a top-level ``folder:``
key in the workflow YAML (so saves, deploys, duplicates, and rollbacks carry
it like ``tags:``), set through PUT .../folder and filtered by ?folder= on
the lists.
"""

import json

import pytest
import yaml

from src.dapier.api import agent as agent_api
from src.dapier.api import admin as admin_api
from src.dapier.api import designer_store as store
from src.dapier.api.admin import routes as admin_routes
from src.dapier.auth import session
from dapier_cli import commands, main as cli_main
from src.dapier.triggers import published_workflows

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


class StubPublishedTable:
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
def published(monkeypatch):
    """The publish table configured and stubbed; the real one never touched."""
    monkeypatch.setenv(published_workflows.TABLE_ENV, "published-test")
    table = StubPublishedTable()
    monkeypatch.setattr(published_workflows, "get_table", lambda table_ref=None: table)
    return table


@pytest.fixture
def github_ready(monkeypatch):
    """Git sync pointed at a scripted GitHub; the token check bypassed."""
    monkeypatch.setenv(store.REPO_URL_ENV, "https://github.com/owner/repo")
    monkeypatch.delenv(store.BRANCH_ENV, raising=False)
    monkeypatch.setattr(store, "get_token", lambda: "test-token")
    return monkeypatch.setattr(
        store, "_github",
        lambda method, path, token, payload=None: GITHUB_SCRIPT[(method, path)],
    )


def seeded_workflow(workflow_id="test-flow", **overrides):
    workflow = {
        "id": workflow_id,
        "enabled": True,
        "trigger": {"connector": "email", "event": "message.received"},
        "actions": [{"id": "a1", "type": "webhook", "url": "https://example.test/hook"}],
    }
    workflow.update(overrides)
    return workflow


# --- validation --------------------------------------------------------------

def test_validate_folder_strips_and_bounds():
    assert store._validate_folder("  Billing  ") == "Billing"
    assert store._validate_folder("") == ""
    assert store._validate_folder("x" * 64) == "x" * 64
    with pytest.raises(store.WorkflowError, match="at most 64"):
        store._validate_folder("x" * 65)
    with pytest.raises(store.WorkflowError, match="must be a string"):
        store._validate_folder(7)
    with pytest.raises(store.WorkflowError, match="flat, not paths"):
        store._validate_folder("a/b")
    with pytest.raises(store.WorkflowError, match="flat, not paths"):
        store._validate_folder("a\\b")
    with pytest.raises(store.WorkflowError, match="flat, not paths"):
        store._validate_folder("..\\escape")


def test_parse_workflow_carries_a_flat_folder():
    workflow = store.parse_workflow(
        "id: filed\nfolder: Invoices\n"
        "trigger: {connector: email, event: message.received}\n"
        "actions: [{type: webhook, url: 'https://x'}]\n")
    assert workflow["folder"] == "Invoices"
    # The canonical dump keeps the folder, so saves/deploys/rollbacks carry it.
    assert "folder: Invoices" in store.workflow_yaml_text(workflow)

    # An empty folder is no folder: the key is dropped, not stored as "".
    workflow = store.parse_workflow(
        "id: filed\nfolder: \"\"\n"
        "trigger: {connector: email, event: message.received}\n"
        "actions: [{type: webhook, url: 'https://x'}]\n")
    assert "folder" not in workflow

    for bad in ("folder: a/b\n", "folder: a\\\\b\n", "folder: 7\n",
                "folder: " + "x" * 70 + "\n"):
        with pytest.raises(store.WorkflowError):
            store.parse_workflow(
                bad +
                "trigger: {connector: email, event: message.received}\n"
                "actions: [{type: webhook, url: 'https://x'}]\n")


# --- the store: api_folder ----------------------------------------------------

def test_folder_set_move_and_clear(github_ready, published):
    published_workflows.publish(seeded_workflow())

    status, payload = store.api_folder("test-flow.yaml", {"folder": " Billing "})
    assert status == 200
    assert payload["folder"] == "Billing"
    assert payload["published"] is True
    assert published.items["test-flow"]["workflow"]["folder"] == "Billing"

    # Moving replaces the previous folder — at most one per workflow.
    status, payload = store.api_folder("test-flow.yaml", {"folder": "Ops"})
    assert status == 200
    assert published.items["test-flow"]["workflow"]["folder"] == "Ops"

    # An empty string clears it.
    status, payload = store.api_folder("test-flow.yaml", {"folder": ""})
    assert status == 200
    assert payload["folder"] == ""
    assert "folder" not in published.items["test-flow"]["workflow"]


def test_folder_is_rejected_before_anything_is_written(github_ready, published):
    published_workflows.publish(seeded_workflow())

    bad_bodies = [
        "Billing",                 # body must be an object
        {},                        # missing the folder key
        {"folder": 7},             # not a string
        {"folder": "a/b"},         # a path, not a folder
        {"folder": "a\\b"},        # back separator
        {"folder": "x" * 65},      # over the length cap
    ]
    for body in bad_bodies:
        status, payload = store.api_folder("test-flow.yaml", body)
        assert status == 400, body
    assert "folder" not in published.items["test-flow"]["workflow"]

    status, _ = store.api_folder("../escape", {"folder": "Ops"})
    assert status == 400
    status, _ = store.api_folder("nope.yaml", {"folder": "Ops"})
    assert status == 404


def test_folder_unconfigured_is_503(monkeypatch):
    monkeypatch.delenv(published_workflows.TABLE_ENV, raising=False)
    status, payload = store.api_folder("test-flow.yaml", {"folder": "Ops"})
    assert status == 503


def test_folder_reports_git_failure_but_stays_live(github_ready, published, monkeypatch):
    published_workflows.publish(seeded_workflow())

    def boom(*args, **kwargs):
        raise store.SyncError("github down")

    monkeypatch.setattr(store, "commit_workflow", boom)

    status, payload = store.api_folder("test-flow.yaml", {"folder": "Ops"})
    assert status == 200
    assert payload["folder"] == "Ops"
    assert "github down" in payload["git_sync_error"]
    assert published.items["test-flow"]["workflow"]["folder"] == "Ops"


def test_list_filters_by_folder_and_summaries_carry_it(github_ready, published):
    published_workflows.publish(seeded_workflow("filed-a", folder="Invoices"))
    published_workflows.publish(seeded_workflow("filed-b", folder="ops"))
    published_workflows.publish(seeded_workflow("unfiled"))

    status, payload = store.api_list()
    assert status == 200
    by_id = {row["id"]: row for row in payload["workflows"]}
    assert by_id["filed-a"]["folder"] == "Invoices"
    assert by_id["filed-b"]["folder"] == "ops"
    assert by_id["unfiled"]["folder"] == ""

    status, payload = store.api_list(folder="invoices")  # case-insensitive exact
    assert status == 200
    assert [row["id"] for row in payload["workflows"] if row["source"].startswith("filed")] \
        == ["filed-a"]

    # The q= search sees the folder name too, like tags.
    status, payload = store.api_list(q="invoices")
    assert any(row["id"] == "filed-a" for row in payload["workflows"])


# --- the agent routes behind the CLI ------------------------------------------

class StubDynamoTable:
    """Just enough DynamoDB for the bearer-token/operator checks."""

    def __init__(self):
        self.items = {}

    def get_item(self, **kwargs):
        key = kwargs["Key"]
        wanted = key.get("token_hash") or key.get("connection_id") or key.get("grantee")
        return {"Item": self.items.get(wanted)} if wanted in self.items else {}

    def put_item(self, **kwargs):
        item = kwargs["Item"]
        self.items[item.get("token_hash") or item.get("connection_id")
                   or item.get("grantee") or item.get("credential_id")] = item

    def update_item(self, **kwargs):
        pass

    def delete_item(self, **kwargs):
        pass

    def query(self, **kwargs):
        return {"Items": []}

    def scan(self, **kwargs):
        return {"Items": list(self.items.values())}


def agent_configure(monkeypatch, email="op@datatalks.club"):
    import boto3

    agent_api.reset_rate_limits()
    tables = {name: StubDynamoTable()
              for name in ("connections", "grants", "credentials", "api-tokens")}

    class Dynamo:
        def Table(self, name):
            return tables[name]

    monkeypatch.setenv("CONNECTIONS_TABLE", "connections")
    monkeypatch.setenv("GRANTS_TABLE", "grants")
    monkeypatch.setenv("CREDENTIALS_TABLE", "credentials")
    monkeypatch.setenv("API_TOKENS_TABLE", "api-tokens")
    monkeypatch.setenv("AUTH_CLI_CLIENT_ID", "cli-client")
    monkeypatch.delenv("AUDIT_TABLE", raising=False)
    monkeypatch.setattr(boto3, "resource", lambda service: Dynamo())
    monkeypatch.setattr(
        agent_api, "verify_id_token",
        lambda token, audience=None: {"sub": "op-1", "email": email},
    )
    return tables


def agent_event(body=None, token="dtc-id-token", query=None):
    headers = {"host": "dapier.example.test"}
    if token is not None:
        headers["authorization"] = f"Bearer {token}"
    request = {"headers": headers, "cookies": []}
    if body is not None:
        request["body"] = json.dumps(body)
    if query is not None:
        request["queryStringParameters"] = query
    return request


def test_agent_folder_route_sets_and_audits(github_ready, published, monkeypatch):
    agent_configure(monkeypatch)
    monkeypatch.setenv("OPERATOR_EMAILS", "op@datatalks.club")
    published_workflows.publish(seeded_workflow())
    audited = []
    monkeypatch.setattr(agent_api.audit, "emit",
                        lambda *args, **kwargs: audited.append(args) or None)

    response = agent_api.route(agent_event({"folder": "Billing"}), "PUT",
                               "/api/agent/designer/workflows/test-flow.yaml/folder")
    assert response["statusCode"] == 200
    body = json.loads(response["body"])
    assert body["folder"] == "Billing" and body["published"] is True
    assert published.items["test-flow"]["workflow"]["folder"] == "Billing"
    assert audited[-1][1] == "workflow.folder"

    # Clearing through the same route.
    response = agent_api.route(agent_event({"folder": ""}), "PUT",
                               "/api/agent/designer/workflows/test-flow.yaml/folder")
    assert response["statusCode"] == 200
    assert "folder" not in published.items["test-flow"]["workflow"]


def test_agent_folder_route_requires_an_operator(github_ready, published, monkeypatch):
    agent_configure(monkeypatch, email="nobody@example.test")
    monkeypatch.setenv("OPERATOR_EMAILS", "op@datatalks.club")
    published_workflows.publish(seeded_workflow())

    response = agent_api.route(agent_event({"folder": "Billing"}), "PUT",
                               "/api/agent/designer/workflows/test-flow.yaml/folder")
    assert response["statusCode"] == 403
    assert published.items["test-flow"]["workflow"].get("folder") is None


def test_agent_folder_route_maps_store_errors(github_ready, published, monkeypatch):
    agent_configure(monkeypatch)
    monkeypatch.setenv("OPERATOR_EMAILS", "op@datatalks.club")
    published_workflows.publish(seeded_workflow())

    bad = agent_api.route(agent_event({"folder": "a/b"}), "PUT",
                          "/api/agent/designer/workflows/test-flow.yaml/folder")
    assert bad["statusCode"] == 400

    missing = agent_api.route(agent_event({"folder": "Ops"}), "PUT",
                              "/api/agent/designer/workflows/nope.yaml/folder")
    assert missing["statusCode"] == 404

    # Only PUT is wired; other methods fall through to the plain 404.
    wrong_method = agent_api.route(agent_event({"folder": "Ops"}), "POST",
                                   "/api/agent/designer/workflows/test-flow.yaml/folder")
    assert wrong_method["statusCode"] == 404


def test_agent_and_admin_lists_forward_the_folder_filter(monkeypatch):
    agent_configure(monkeypatch)
    seen = {}
    monkeypatch.setattr(store, "api_list",
                        lambda q=None, tag=None, folder=None, visible=None:
                        seen.setdefault("folders", []).append(folder)
                        or (200, {"workflows": [], "git_sync": {}}))
    agent_api.route(agent_event(query={"folder": "Billing"}), "GET",
                    "/api/agent/designer/workflows")
    assert seen["folders"] == ["Billing"]
    assert agent_api.route(agent_event(query={"tag": "ops"}), "GET",
                           "/api/agent/designer/workflows")["statusCode"] == 200
    assert seen["folders"] == ["Billing", None]


# --- the admin routes behind the console --------------------------------------

def admin_request(method, path, body=None):
    return {
        "requestContext": {"http": {"method": method, "path": path}},
        "headers": {"host": "dapier.example.test"},
        "cookies": [],
        "queryStringParameters": {},
        "body": json.dumps(body) if body is not None else None,
    }


@pytest.fixture
def operator_session(monkeypatch):
    monkeypatch.setattr(session, "authenticated", lambda event: True)
    monkeypatch.setattr(session, "_csrf_ok", lambda event, method: True)
    monkeypatch.setattr(session, "require_operator", lambda event: ({"sub": "op-1"}, None))
    monkeypatch.setattr(session, "_audit_event", lambda *args, **kwargs: None)


def test_admin_folder_route_sets_and_audits(github_ready, published, operator_session,
                                            monkeypatch):
    published_workflows.publish(seeded_workflow())
    audited = []
    monkeypatch.setattr(session, "_audit_event",
                        lambda *args, **kwargs: audited.append(args) or None)

    response = admin_api.route(
        admin_request("PUT", "/api/admin/designer/workflows/test-flow.yaml/folder",
                      {"folder": "Billing"}),
        "PUT", "/api/admin/designer/workflows/test-flow.yaml/folder",
    )
    assert response["statusCode"] == 200
    body = json.loads(response["body"])
    assert body["folder"] == "Billing" and body["published"] is True
    assert published.items["test-flow"]["workflow"]["folder"] == "Billing"
    assert audited[-1][1] == "workflow.folder"

    refused = admin_api.route(
        admin_request("PUT", "/api/admin/designer/workflows/../escape/folder",
                      {"folder": "Billing"}),
        "PUT", "/api/admin/designer/workflows/../escape/folder",
    )
    assert refused["statusCode"] == 404  # never matches the route pattern


def test_overview_surfaces_folder_and_filters_by_it(monkeypatch):
    from src.dapier.api import overview as overview_api

    views = [
        {"id": "filed-a", "description": "", "enabled": True,
         "trigger": {"connector": "email", "event": "message.received"},
         "triggerCount": 1, "actions": [{"type": "slack"}], "published": False,
         "folder": "Invoices"},
        {"id": "unfiled", "description": "", "enabled": True,
         "trigger": {"connector": "schedule", "event": "tick"},
         "triggerCount": 1, "actions": [{"type": "dropbox_upload"}], "published": False,
         "folder": ""},
    ]
    monkeypatch.setattr(overview_api, "_workflows", lambda *args, **kwargs: views)
    monkeypatch.setattr(overview_api, "_scan", lambda *args, **kwargs: [])
    monkeypatch.setenv("EXECUTIONS_TABLE", "executions")
    monkeypatch.setenv("CONNECTIONS_TABLE", "connections")
    monkeypatch.setattr(overview_api, "_credential_status", lambda provider: {"provider": provider})
    monkeypatch.setattr(overview_api, "_oauth_client_status", lambda provider: {"provider": provider})
    monkeypatch.setattr(overview_api.api_tokens, "list_all", lambda: [])
    monkeypatch.setattr(overview_api, "_email_triggers",
                        lambda: {"domain": "", "triggers": [], "managed_routes": []})
    monkeypatch.setattr(overview_api.runs, "recent", lambda *args, **kwargs: [])
    monkeypatch.delenv("TASK_USAGE_TABLE", raising=False)
    event = admin_request("GET", "/api/admin/overview")

    payload = json.loads(overview_api.overview(event)["body"])
    by_id = {row["id"]: row for row in payload["workflows"]}
    assert by_id["filed-a"]["folder"] == "Invoices"
    assert by_id["unfiled"]["folder"] == ""
    assert payload["workflow_folders"] == ["Invoices"]

    event["queryStringParameters"] = {"folder": "invoices"}  # case-insensitive exact
    narrowed = json.loads(overview_api.overview(event)["body"])
    assert [row["id"] for row in narrowed["workflows"]] == ["filed-a"]


# --- the CLI commands ----------------------------------------------------------

@pytest.fixture
def isolated_home(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.delenv("DAPIER_API_URL", raising=False)
    return tmp_path


def test_cli_workflows_folder_sets_and_clears(isolated_home, monkeypatch, capsys):
    calls = []

    def fake_call(api_url, method, path, body=None, **kwargs):
        calls.append((method, path, body))
        return {"file": "test-flow.yaml", "folder": body["folder"], "published": True}

    monkeypatch.setattr(commands.api, "call", fake_call)

    rc = cli_main.main(["workflows", "folder", "test-flow.yaml", "--set", "Billing"])

    assert rc == 0
    assert calls[0] == ("PUT", "/api/agent/designer/workflows/test-flow.yaml/folder",
                        {"folder": "Billing"})
    assert "Billing" in capsys.readouterr().out

    rc = cli_main.main(["workflows", "folder", "test-flow.yaml", "--clear"])
    assert rc == 0
    assert calls[1][2] == {"folder": ""}
    assert "(none)" in capsys.readouterr().out


def test_cli_workflows_folder_usage_errors(isolated_home, monkeypatch, capsys):
    def fake_call(api_url, method, path, body=None, **kwargs):
        raise AssertionError("no call expected")

    monkeypatch.setattr(commands.api, "call", fake_call)

    rc = cli_main.main(["workflows", "folder", "test-flow.yaml"])
    assert rc == 2
    assert "Nothing to do" in capsys.readouterr().out

    rc = cli_main.main(["workflows", "folder", "test-flow.yaml",
                        "--set", "A", "--clear"])
    assert rc == 2
    assert "--set" in capsys.readouterr().out


def test_cli_workflows_folder_warns_when_git_sync_failed(isolated_home, monkeypatch, capsys):
    monkeypatch.setattr(commands.api, "call",
                        lambda *args, **kwargs: {"file": "f.yaml", "folder": "Ops",
                                                 "published": True,
                                                 "git_sync_error": "github down"})

    rc = cli_main.main(["workflows", "folder", "f.yaml", "--set", "Ops"])

    assert rc == 0
    assert "github down" in capsys.readouterr().out


def test_cli_workflows_list_forwards_the_folder_filter(isolated_home, monkeypatch, capsys):
    calls = []

    def fake_call(api_url, method, path, body=None, **kwargs):
        calls.append(path)
        return {"workflows": [{"id": "filed", "source": "filed.yaml", "enabled": True,
                               "connector": "email", "event": "message.received",
                               "actionCount": 1, "folder": "Billing", "tags": ["ops"]}],
                "git_sync": {"configured": False, "repo": "r", "branch": "main"}}

    monkeypatch.setattr(commands.api, "call", fake_call)

    rc = cli_main.main(["workflows", "list", "--folder", "Billing"])

    assert rc == 0
    assert calls == ["/api/agent/designer/workflows?folder=Billing"]
    out = capsys.readouterr().out
    assert "filed" in out and "[folder: Billing]" in out and "[ops]" in out
