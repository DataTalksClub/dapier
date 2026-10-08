"""Workflow delete and tags on every surface: the designer store, the admin
and agent routes behind the console and CLI, and the CLI commands themselves.

Delete = unpublish the live item, then one atomic git tree-delete commit,
refused 409 while runs are parked on a delay. Tags = Zapier-style
organization labels stored in the workflow YAML, set through PUT .../tags
and filtered by ?tag= on the list.
"""

import json

import pytest

from src.dapier.api import agent as agent_api
from src.dapier.api import designer_store
from src.dapier.api import runs as runs_api
from src.dapier.api.admin import routes as admin_routes
from dapier_cli import commands, main as cli_main
from src.dapier.triggers import published_workflows

WORKFLOW_YAML = """\
id: test-flow
enabled: true
trigger:
  connector: email
  event: message.received
actions:
  - id: a1
    type: webhook
    url: https://example.test/hook
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
    monkeypatch.setenv(designer_store.REPO_URL_ENV, "https://github.com/owner/repo")
    monkeypatch.delenv(designer_store.BRANCH_ENV, raising=False)
    monkeypatch.setattr(designer_store, "get_token", lambda: "test-token")
    return monkeypatch.setattr(
        designer_store, "_github",
        lambda method, path, token, payload=None: GITHUB_SCRIPT[(method, path)],
    )


@pytest.fixture
def no_delayed_runs(monkeypatch):
    monkeypatch.setattr(runs_api, "delayed_runs", lambda workflow_id, limit=25: [])


def seeded_workflow(**overrides):
    workflow = {
        "id": "test-flow",
        "enabled": True,
        "trigger": {"connector": "email", "event": "message.received"},
        "actions": [{"id": "a1", "type": "webhook", "url": "https://example.test/hook"}],
    }
    workflow.update(overrides)
    return workflow


# --- the store: delete ------------------------------------------------------

def test_delete_unpublishes_and_commits_removal(github_ready, published, no_delayed_runs):
    published_workflows.publish(seeded_workflow())

    status, payload = designer_store.api_delete("test-flow.yaml")

    assert status == 200
    assert payload["deleted"] is True
    assert payload["was_published"] is True
    assert payload["published"] is False
    assert payload["commit"] == "commit456"
    assert "test-flow" not in published.items


def test_delete_refuses_while_runs_are_delayed(github_ready, published, monkeypatch):
    published_workflows.publish(seeded_workflow())
    monkeypatch.setattr(runs_api, "delayed_runs",
                        lambda workflow_id, limit=25: [{"run_id": "run-1", "delayed_until": "soon"}])

    status, payload = designer_store.api_delete("test-flow.yaml")

    assert status == 409
    assert payload["delayed_runs"][0]["run_id"] == "run-1"
    assert "test-flow" in published.items  # nothing was torn down


def test_delete_reports_git_failure_but_stays_deleted(github_ready, published, no_delayed_runs,
                                                      monkeypatch):
    published_workflows.publish(seeded_workflow())

    def boom(*args, **kwargs):
        raise designer_store.SyncError("github down")

    monkeypatch.setattr(designer_store, "commit_delete", boom)

    status, payload = designer_store.api_delete("test-flow.yaml")

    assert status == 200
    assert payload["deleted"] is True
    assert "github down" in payload["git_sync_error"]
    assert "test-flow" not in published.items


def test_delete_missing_workflow_is_404(github_ready, published, no_delayed_runs):
    status, payload = designer_store.api_delete("nope.yaml")
    assert status == 404
    assert "no such workflow" in payload["error"]


def test_delete_invalid_name_is_400(github_ready, no_delayed_runs):
    status, payload = designer_store.api_delete("../escape")
    assert status == 400


def test_commit_delete_removes_the_file_in_one_tree(github_ready, monkeypatch):
    calls = []

    def fake(method, path, token, payload=None):
        calls.append((method, path, payload))
        return GITHUB_SCRIPT[(method, path)]

    monkeypatch.setattr(designer_store, "_github", fake)

    result = designer_store.commit_delete("gone.yaml", message="designer: delete workflow gone")

    assert result["commit"] == "commit456"
    tree_payload = next(payload for method, path, payload in calls
                        if method == "POST" and path.endswith("/git/trees"))
    assert tree_payload["tree"] == [
        {"path": "workflows/gone.yaml", "mode": "100644", "type": "blob", "sha": None}]


# --- the store: tags --------------------------------------------------------

def test_tags_set_filter_and_clear(github_ready, published):
    published_workflows.publish(seeded_workflow())

    status, payload = designer_store.api_tags("test-flow.yaml", {"tags": [" Billing ", "ops"]})

    assert status == 200
    assert payload["tags"] == ["billing", "ops"]  # normalized to lowercase
    assert payload["published"] is True
    assert published.items["test-flow"]["workflow"]["tags"] == ["billing", "ops"]

    status, payload = designer_store.api_list(tag="billing")
    assert status == 200
    assert [item["id"] for item in payload["workflows"]] == ["test-flow"]

    status, payload = designer_store.api_tags("test-flow.yaml", {"tags": []})
    assert status == 200
    assert "tags" not in published.items["test-flow"]["workflow"]
    status, payload = designer_store.api_list(tag="billing")
    assert payload["workflows"] == []


def test_tags_are_rejected_before_anything_is_written(github_ready, published):
    published_workflows.publish(seeded_workflow())

    bad_bodies = [
        ["billing"],                          # body must be an object
        {"nope": ["billing"]},                # missing the tags key
        {"tags": "billing"},                  # a bare string is not a list
        {"tags": [1]},                        # non-string entries
        {"tags": ["ok", ""]},                 # empty tag
        {"tags": ["x" * 65]},                 # over the length cap
        {"tags": [str(i) for i in range(21)]},  # over the count cap
    ]
    for body in bad_bodies:
        status, payload = designer_store.api_tags("test-flow.yaml", body)
        assert status == 400, body
    assert "tags" not in published.items["test-flow"]["workflow"]

    status, _ = designer_store.api_tags("../escape", {"tags": ["ops"]})
    assert status == 400
    status, _ = designer_store.api_tags("nope.yaml", {"tags": ["ops"]})
    assert status == 404


def test_tags_unconfigured_is_503(monkeypatch):
    monkeypatch.delenv(published_workflows.TABLE_ENV, raising=False)
    status, payload = designer_store.api_tags("test-flow.yaml", {"tags": ["ops"]})
    assert status == 503


# --- the agent routes behind the CLI ----------------------------------------

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


def test_agent_delete_and_tags_routes(github_ready, published, monkeypatch, no_delayed_runs):
    agent_configure(monkeypatch)
    monkeypatch.setenv("OPERATOR_EMAILS", "op@datatalks.club")
    published_workflows.publish(seeded_workflow())

    deleted = agent_api.route(agent_event(), "DELETE",
                              "/api/agent/designer/workflows/test-flow.yaml")
    assert deleted["statusCode"] == 200
    body = json.loads(deleted["body"])
    assert body["deleted"] is True and body["commit"] == "commit456"
    assert "test-flow" not in published.items

    published_workflows.publish(seeded_workflow())
    tagged = agent_api.route(agent_event({"tags": ["ops"]}), "PUT",
                             "/api/agent/designer/workflows/test-flow.yaml/tags")
    assert tagged["statusCode"] == 200
    assert json.loads(tagged["body"])["tags"] == ["ops"]


def test_agent_delete_requires_an_operator(github_ready, published, monkeypatch):
    agent_configure(monkeypatch, email="nobody@example.test")
    monkeypatch.setenv("OPERATOR_EMAILS", "op@datatalks.club")

    response = agent_api.route(agent_event(), "DELETE",
                               "/api/agent/designer/workflows/test-flow.yaml")
    assert response["statusCode"] == 403


# --- the admin routes behind the console ------------------------------------

def admin_request(method, body=None):
    return {
        "requestContext": {"http": {"method": method}},
        "queryStringParameters": {},
        "body": json.dumps(body) if body is not None else None,
    }


def test_admin_delete_and_tags_routes(github_ready, published, no_delayed_runs):
    published_workflows.publish(seeded_workflow())

    deleted = admin_routes.delete_designer_workflow(
        admin_request("DELETE"), "op", "test-flow.yaml")
    assert deleted["statusCode"] == 200
    assert json.loads(deleted["body"])["deleted"] is True
    assert "test-flow" not in published.items

    published_workflows.publish(seeded_workflow())
    tagged = admin_routes.tags_designer_workflow(
        admin_request("PUT", {"tags": ["ops"]}), "op", "test-flow.yaml")
    assert tagged["statusCode"] == 200
    assert json.loads(tagged["body"])["tags"] == ["ops"]

    refused = admin_routes.delete_designer_workflow(
        admin_request("DELETE"), "op", "../escape")
    assert refused["statusCode"] == 400


# --- the CLI commands --------------------------------------------------------

@pytest.fixture
def isolated_home(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.delenv("DAPIER_API_URL", raising=False)
    return tmp_path


def test_cli_workflows_delete_confirms_then_deletes(isolated_home, monkeypatch, capsys):
    calls = []

    def fake_call(api_url, method, path, body=None, **kwargs):
        calls.append((method, path))
        return {"file": "test-flow.yaml", "deleted": True, "commit": "commit456"}

    monkeypatch.setattr(commands.api, "call", fake_call)
    monkeypatch.setattr("builtins.input", lambda prompt: "y")

    rc = cli_main.main(["workflows", "delete", "test-flow.yaml"])

    assert rc == 0
    assert calls == [("DELETE", "/api/agent/designer/workflows/test-flow.yaml")]
    out = capsys.readouterr().out
    assert "Deleted test-flow.yaml" in out and "Committed the removal" in out


def test_cli_workflows_delete_cancelled_without_yes(isolated_home, monkeypatch, capsys):
    calls = []

    def fake_call(api_url, method, path, body=None, **kwargs):
        calls.append((method, path))
        return {}

    monkeypatch.setattr(commands.api, "call", fake_call)
    monkeypatch.setattr("builtins.input", lambda prompt: "n")

    rc = cli_main.main(["workflows", "delete", "test-flow.yaml"])

    assert rc == 1
    assert calls == []
    assert "Cancelled" in capsys.readouterr().out


def test_cli_workflows_delete_assume_yes_skips_the_prompt(isolated_home, monkeypatch):
    calls = []

    def fake_call(api_url, method, path, body=None, **kwargs):
        calls.append((method, path))
        return {"file": "test-flow.yaml", "deleted": True}

    monkeypatch.setattr(commands.api, "call", fake_call)

    rc = cli_main.main(["workflows", "delete", "test-flow.yaml", "--yes"])

    assert rc == 0
    assert calls == [("DELETE", "/api/agent/designer/workflows/test-flow.yaml")]


def test_cli_workflows_delete_warns_when_git_sync_failed(isolated_home, monkeypatch, capsys):
    monkeypatch.setattr(commands.api, "call",
                        lambda *args, **kwargs: {"file": "f.yaml", "deleted": True,
                                                 "git_sync_error": "github down"})

    rc = cli_main.main(["workflows", "delete", "f.yaml", "--yes"])

    assert rc == 0
    assert "github down" in capsys.readouterr().out


def test_cli_workflows_tags_sets_and_clears(isolated_home, monkeypatch, capsys):
    calls = []

    def fake_call(api_url, method, path, body=None, **kwargs):
        calls.append((method, path, body))
        return {"file": "test-flow.yaml", "tags": body["tags"], "published": True}

    monkeypatch.setattr(commands.api, "call", fake_call)

    rc = cli_main.main(["workflows", "tags", "test-flow.yaml", "--tags", "ops, billing"])

    assert rc == 0
    assert calls[0] == ("PUT", "/api/agent/designer/workflows/test-flow.yaml/tags",
                        {"tags": ["ops", "billing"]})
    assert "ops, billing" in capsys.readouterr().out

    rc = cli_main.main(["workflows", "tags", "test-flow.yaml", "--clear"])
    assert rc == 0
    assert calls[1][2] == {"tags": []}


def test_cli_workflows_tags_without_args_is_a_usage_error(isolated_home, monkeypatch, capsys):
    def fake_call(api_url, method, path, body=None, **kwargs):
        raise AssertionError("no call expected")

    monkeypatch.setattr(commands.api, "call", fake_call)

    rc = cli_main.main(["workflows", "tags", "test-flow.yaml"])

    assert rc == 2
    assert "Nothing to do" in capsys.readouterr().out


def test_cli_workflows_list_forwards_the_tag_filter(isolated_home, monkeypatch, capsys):
    calls = []

    def fake_call(api_url, method, path, body=None, **kwargs):
        calls.append(path)
        return {"workflows": [{"id": "ops-flow", "source": "ops-flow.yaml", "enabled": True,
                               "connector": "email", "event": "message.received",
                               "actionCount": 1, "tags": ["ops"]}],
                "git_sync": {"configured": False, "repo": "r", "branch": "main"}}

    monkeypatch.setattr(commands.api, "call", fake_call)

    rc = cli_main.main(["workflows", "list", "--tag", "ops"])

    assert rc == 0
    assert calls == ["/api/agent/designer/workflows?tag=ops"]
    out = capsys.readouterr().out
    assert "ops-flow" in out and "[ops]" in out
