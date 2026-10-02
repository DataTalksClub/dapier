"""Duplicate a workflow: copy under a new id, publish the copy, leave the
original untouched — the domain path, both route surfaces, and the CLI."""

import json

import pytest
import yaml

from src.dapier.api import designer_store
from src.dapier.triggers import published_workflows

WORKFLOW_YAML = """\
allow_email_overlap: true
id: test-flow
enabled: true
trigger:
  connector: email
  event: message.received
  filters:
    route:
      equals: test-flow
actions:
  - id: a1
    type: webhook
    url: https://example.test/hook
  - id: a2
    type: mystery_action
    foo:
      bar: 1
"""

RUN_STATE_YAML = """\
allow_email_overlap: true
id: test-flow
enabled: true
last_run: 2026-01-01T00:00:00Z
run_count: 7
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
    monkeypatch.setenv(designer_store.TOKEN_SECRET_ENV, "test-secret")
    monkeypatch.setattr(designer_store, "get_token", lambda: "test-token")
    calls = []

    def fake(method, path, token, payload=None):
        calls.append((method, path, token, payload))
        return GITHUB_SCRIPT[(method, path)]

    monkeypatch.setattr(designer_store, "_github", fake)
    return calls


@pytest.fixture
def bundle(tmp_path, published):
    """The source workflow in the managed store."""
    published_workflows.publish(designer_store.parse_workflow(WORKFLOW_YAML))
    return tmp_path


def committed_tree_yaml(github_calls):
    """The YAML the duplicate committed, parsed back."""
    tree = next(payload["tree"] for method, path, _, payload in github_calls
                if method == "POST" and path.endswith("/git/trees"))
    return tree, yaml.safe_load(tree[0]["content"])


def test_duplicate_copies_under_a_new_id_and_publishes(published, github_ready, bundle):
    status, payload = designer_store.api_duplicate("test-flow.yaml", {}, operator="op-1")

    assert status == 200
    assert payload["file"] == "test-flow-copy.yaml"
    assert payload["duplicated_from"] == "test-flow.yaml"
    assert payload["commit"] == "commit456"
    assert payload["published"] is True
    assert payload["removed"] is None  # the original file stays

    tree, committed = committed_tree_yaml(github_ready)
    assert [entry["path"] for entry in tree] == ["workflows/test-flow-copy.yaml"]
    assert committed["id"] == "test-flow-copy"
    assert committed["trigger"]["connector"] == "email"
    assert [action["id"] for action in committed["actions"]] == ["a1", "a2"]

    # The copy is live; the original's published item is untouched.
    assert "test-flow-copy" in published.items
    assert published.items["test-flow-copy"]["workflow"]["id"] == "test-flow-copy"
    assert published.items["test-flow-copy"]["published_by"] == "op-1"
    assert published.items["test-flow-copy"]["owner"] == "op-1"  # G17: the caller owns the copy
    assert published.items["test-flow"]["workflow"] == designer_store.parse_workflow(WORKFLOW_YAML)
    assert github_ready[-1] == ("PATCH", "/repos/owner/repo/git/refs/heads/main", "test-token", {"sha": "commit456"})


def test_duplicate_accepts_an_explicit_name_and_slugifies_it(published, github_ready, bundle):
    status, payload = designer_store.api_duplicate("test-flow.yaml", {"name": "My Backup Flow!"})

    assert status == 200
    assert payload["file"] == "my-backup-flow.yaml"
    _, committed = committed_tree_yaml(github_ready)
    assert committed["id"] == "my-backup-flow"


def test_duplicate_strips_run_state_keys(published, github_ready, bundle):
    published_workflows.publish(designer_store.parse_workflow(RUN_STATE_YAML),
                                previous=published_workflows.get_item("test-flow"))

    status, _ = designer_store.api_duplicate("test-flow.yaml")

    assert status == 200
    _, committed = committed_tree_yaml(github_ready)
    assert "last_run" not in committed
    assert "run_count" not in committed
    assert committed["id"] == "test-flow-copy"
    assert committed["actions"] == [{"id": "a1", "type": "webhook", "url": "https://example.test/hook"}]


@pytest.mark.parametrize("extra_file,extra_yaml,body", [
    # The default <id>-copy name is already taken.
    ("test-flow-copy.yaml", WORKFLOW_YAML.replace("id: test-flow", "id: test-flow-copy"), None),
    # The explicit name collides with another workflow.
    ("other.yaml", "id: other\ntrigger: {connector: email, event: e}\n"
                   "actions: [{id: a, type: webhook, url: 'https://x'}]\n", {"name": "other"}),
    # A name that slugifies back onto the source itself.
    (None, None, {"name": "Test Flow"}),
])
def test_duplicate_name_collisions_return_409(published, github_ready, bundle,
                                              extra_file, extra_yaml, body):
    if extra_file:
        published_workflows.publish(designer_store.parse_workflow(extra_yaml))

    status, payload = designer_store.api_duplicate("test-flow.yaml", body)

    assert status == 409
    assert "already exists" in payload["error"]
    assert not any(method == "POST" for method, _, _, _ in github_ready)
    assert published.items["test-flow"]["workflow"]["id"] == "test-flow"


def test_duplicate_missing_source_is_404(published, github_ready, bundle):
    status, payload = designer_store.api_duplicate("nope.yaml")
    assert status == 404
    assert "no such workflow" in payload["error"]


@pytest.mark.parametrize("source,body,fragment", [
    ("test-flow.yaml", {"name": "!!!"}, "cannot derive"),
    ("test-flow.yaml", {"name": "   "}, "non-empty string"),
    ("test-flow.yaml", {"name": 5}, "non-empty string"),
    ("test-flow.yaml", ["not", "an", "object"], "request body"),
    ("../escape.yaml", None, "invalid workflow file name"),
    ("no-extension", None, "invalid workflow file name"),
])
def test_duplicate_rejects_bad_input(published, github_ready, bundle, source, body, fragment):
    status, payload = designer_store.api_duplicate(source, body)
    assert status == 400
    assert fragment in payload["error"]


@pytest.mark.parametrize("text,expected", [
    ("My Backup Flow!", "my-backup-flow"),
    ("  Already_Snake  ", "already_snake"),
    ("spaces   and---dashes", "spaces-and-dashes"),
    ("-- Leading underscore_9", "leading-underscore_9"),
    ("x" * 80, "x" * 63),
    ("!!!", ""),
])
def test_slugify_id(text, expected):
    assert designer_store.slugify_id(text) == expected


# ---- Admin + agent routes ----

from src.dapier.api import admin
from src.dapier.api import agent as agent_api
from src.dapier.auth import session


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
    monkeypatch.setattr(session, "_audit_event", lambda *args, **kwargs: None)


def agent_request(method, path, body=None, token="dtc-token"):
    request = {
        "requestContext": {"http": {"method": method, "path": path}},
        "headers": {"host": "dapier.example.test"},
        "cookies": [],
    }
    if token is not None:
        request["headers"]["authorization"] = f"Bearer {token}"
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
    monkeypatch.setattr(agent_api, "audit", type("Audit", (), {
        "emit": staticmethod(lambda *args, **kwargs: None),
    }))


def test_admin_duplicate_route_drives_the_same_store(monkeypatch, operator_session):
    calls = []
    monkeypatch.setattr(designer_store, "api_duplicate", lambda source, body, operator=None:
                        calls.append({"source": source, "body": body, "operator": operator})
                        or (200, {"file": "test-flow-copy.yaml", "duplicated_from": source}))
    audits = []
    monkeypatch.setattr(session, "_audit_event",
                        lambda *args, **kwargs: audits.append((args, kwargs)) or None)

    response = admin.route(
        admin_request("POST", "/api/admin/designer/workflows/test-flow.yaml/duplicate",
                      {"name": "copy"}),
        "POST", "/api/admin/designer/workflows/test-flow.yaml/duplicate",
    )
    assert response["statusCode"] == 200
    assert calls == [{"source": "test-flow.yaml", "body": {"name": "copy"}, "operator": "op-1"}]
    assert audits[0][0][1] == "workflow.duplicate"


def test_agent_duplicate_route_drives_the_same_store(monkeypatch, agent_identity):
    calls = []
    monkeypatch.setattr(designer_store, "api_duplicate", lambda source, body, operator=None:
                        calls.append({"source": source, "body": body, "operator": operator})
                        or (200, {"file": "test-flow-copy.yaml", "duplicated_from": source}))

    response = agent_api.route(
        agent_request("POST", "/api/agent/designer/workflows/test-flow.yaml/duplicate",
                      {"name": "copy"}),
        "POST", "/api/agent/designer/workflows/test-flow.yaml/duplicate",
    )
    assert response["statusCode"] == 200
    assert calls == [{"source": "test-flow.yaml", "body": {"name": "copy"}, "operator": "agent-op"}]

    monkeypatch.setattr(
        agent_api, "verify_id_token",
        lambda token, audience=None: (_ for _ in ()).throw(ValueError("bad")),
    )
    assert agent_api.route(
        agent_request("POST", "/api/agent/designer/workflows/test-flow.yaml/duplicate",
                      token="expired"),
        "POST", "/api/agent/designer/workflows/test-flow.yaml/duplicate",
    )["statusCode"] == 401


def test_agent_duplicate_end_to_end_publishes_the_copy(published, github_ready, bundle, agent_identity):
    response = agent_api.route(
        agent_request("POST", "/api/agent/designer/workflows/test-flow.yaml/duplicate", {}),
        "POST", "/api/agent/designer/workflows/test-flow.yaml/duplicate",
    )
    assert response["statusCode"] == 200
    payload = json.loads(response["body"])
    assert payload["file"] == "test-flow-copy.yaml"
    assert payload["published"] is True
    assert payload["duplicated_from"] == "test-flow.yaml"
    assert "test-flow-copy" in published.items
    assert "test-flow" in published.items


# ---- CLI ----

from dapier_cli import commands as cli_commands


def test_cli_duplicate_calls_the_agent_endpoint(monkeypatch, capsys):
    seen = {}

    def fake_call(api_url, method, path, body=None, **kwargs):
        seen.update(method=method, path=path, body=body)
        return {"file": "my-copy.yaml", "commit": "abc1234", "published": True,
                "duplicated_from": "test-flow.yaml"}

    monkeypatch.setattr(cli_commands.api, "call", fake_call)
    assert cli_commands.workflows_duplicate(
        "https://api.example.test", "test-flow.yaml", name="My Copy") == 0
    assert (seen["method"], seen["path"]) == (
        "POST", "/api/agent/designer/workflows/test-flow.yaml/duplicate")
    assert seen["body"] == {"name": "My Copy"}
    out, _ = capsys.readouterr()
    assert "Duplicated test-flow.yaml as my-copy.yaml" in out
    assert "published it live" in out

    monkeypatch.setattr(cli_commands.api, "call", fake_call)
    assert cli_commands.workflows_duplicate("https://api.example.test", "test-flow.yaml") == 0
    assert seen["body"] == {}  # no --name: the server defaults to <id>-copy
