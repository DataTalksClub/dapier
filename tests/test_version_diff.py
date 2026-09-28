"""Version diff: the rollback-confidence half of the versions list.

Domain (published_workflows.diff_versions + designer_store.api_diff): two
published revisions resolve to their canonical YAML texts and one bounded
unified diff. Both API surfaces expose
GET /api/{admin,agent}/designer/workflows/<file>/versions/diff?from=&to=
gated like the versions read, the CLI prints the same text through
`dapier workflows diff <file> --from <rev> --to <rev>`, and the console's
Versions dialog renders it behind its Diff button.
"""

import json
from pathlib import Path

import pytest

from src.dapier.api import admin
from src.dapier.api import agent as agent_api
from src.dapier.api import designer_store
from src.dapier.auth import roles, session
from src.dapier.triggers import published_workflows

WEB = Path(__file__).resolve().parents[1] / "src" / "web"

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


def revised(yaml_text, url):
    return yaml_text.replace("https://example.test/hook", url)


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


@pytest.fixture
def two_revisions(git_sync, history_store):
    assert designer_store.api_save({"yaml": WORKFLOW_YAML}, operator="op-1", live=True)[0] == 200
    assert designer_store.api_save(
        {"yaml": revised(WORKFLOW_YAML, "https://example.test/hook2")}, operator="op-2", live=True)[0] == 200


# ---- Domain ----


def test_diff_versions_returns_both_records(two_revisions):
    pair = published_workflows.diff_versions("test-flow", 1, 2)
    assert pair["from"]["revision"] == 1
    assert pair["to"]["revision"] == 2
    assert pair["from"]["workflow"]["actions"][0]["url"] == "https://example.test/hook"
    assert pair["to"]["workflow"]["actions"][0]["url"] == "https://example.test/hook2"


def test_diff_versions_with_a_missing_revision_is_none(two_revisions):
    assert published_workflows.diff_versions("test-flow", 1, 9) is None
    assert published_workflows.diff_versions("test-flow", 0, 1) is None
    assert published_workflows.diff_versions("ghost", 1, 2) is None


# ---- designer_store.api_diff ----


def test_api_diff_renders_the_unified_diff_between_revisions(two_revisions):
    status, payload = designer_store.api_diff("test-flow.yaml", "1", "2")
    assert status == 200
    assert payload["file"] == "test-flow.yaml"
    assert payload["workflow"] == "test-flow"
    assert payload["from"] == {"revision": 1, "yaml": designer_store.workflow_yaml_text(
        published_workflows.get_version("test-flow", 1)["workflow"])}
    assert payload["to"]["revision"] == 2
    assert "-  url: https://example.test/hook\n" in payload["diff"]
    assert "+  url: https://example.test/hook2\n" in payload["diff"]
    assert payload["diff"].startswith("--- test-flow.yaml v1")
    assert "+++ test-flow.yaml v2" in payload["diff"]
    assert payload["same"] is False
    assert payload["truncated"] is False


def test_api_diff_flags_identical_definitions(two_revisions):
    status, payload = designer_store.api_diff("test-flow.yaml", 1, 1)
    assert status == 200
    assert payload["same"] is True
    assert payload["diff"] == ""
    assert payload["from"]["yaml"] == payload["to"]["yaml"]


def test_api_diff_answers_for_a_deleted_workflow_by_id(git_sync, history_store):
    assert designer_store.api_save({"yaml": WORKFLOW_YAML}, operator="op-1", live=True)[0] == 200
    # The version records outlive a delete of the live item.
    published_workflows.unpublish("test-flow")
    status, payload = designer_store.api_diff("test-flow.yaml", 1, 1)
    assert status == 200
    assert payload["workflow"] == "test-flow"
    assert payload["same"] is True


@pytest.mark.parametrize("from_revision, to_revision, fragment", [
    (None, "2", "from and to must be version numbers"),
    ("1", None, "from and to must be version numbers"),
    ("x", "2", "from and to must be version numbers"),
    ("1", "9", "no version 1 or version 9 of workflow test-flow"),
])
def test_api_diff_rejects_bad_revisions(two_revisions, from_revision, to_revision, fragment):
    status, payload = designer_store.api_diff("test-flow.yaml", from_revision, to_revision)
    assert status in (400, 404)
    assert fragment in payload["error"]


def test_api_diff_rejects_an_invalid_file_name(two_revisions):
    status, payload = designer_store.api_diff("../escape.yaml", 1, 2)
    assert status == 400
    assert "invalid workflow file name" in payload["error"]


def test_api_diff_needs_the_published_store(monkeypatch):
    monkeypatch.delenv(published_workflows.TABLE_ENV, raising=False)
    status, payload = designer_store.api_diff("test-flow.yaml", 1, 2)
    assert status == 503


def test_api_diff_caps_the_body_and_flags_truncation(two_revisions, monkeypatch):
    monkeypatch.setattr(designer_store, "MAX_DIFF_CHARS", 40)
    status, payload = designer_store.api_diff("test-flow.yaml", 1, 2)
    assert status == 200
    assert len(payload["diff"]) == 40
    assert payload["truncated"] is True


# ---- Console (admin) surface ----


def admin_request(method, path, body=None, query=None):
    return {
        "requestContext": {"http": {"method": method, "path": path}},
        "headers": {"host": "dapier.example.test"},
        "cookies": [],
        "queryStringParameters": query or {},
        "body": json.dumps(body) if body is not None else None,
    }


@pytest.fixture
def operator_session(monkeypatch):
    monkeypatch.setattr(session, "authenticated", lambda event: True)
    monkeypatch.setattr(session, "_csrf_ok", lambda event, method: True)
    monkeypatch.setattr(session, "require_role",
                        lambda event, minimum="operator": ({"sub": "op-1"}, None))


DIFF_PATH = "/api/admin/designer/workflows/test-flow.yaml/versions/diff"


def test_admin_diff_routes_to_the_store_with_the_query(monkeypatch, operator_session):
    seen = {}

    def fake_api_diff(source, from_revision, to_revision, visible=None):
        seen.update(source=source, frm=from_revision, to=to_revision)
        return 200, {"diff": ""}

    monkeypatch.setattr(designer_store, "api_diff", fake_api_diff)
    response = admin.route(
        admin_request("GET", DIFF_PATH, query={"from": "1", "to": "2"}),
        "GET", DIFF_PATH,
    )
    assert response["statusCode"] == 200
    assert seen == {"source": "test-flow.yaml", "frm": "1", "to": "2"}


def test_admin_diff_serves_the_payload(two_revisions, operator_session):
    response = admin.route(
        admin_request("GET", DIFF_PATH, query={"from": "1", "to": "2"}),
        "GET", DIFF_PATH,
    )
    assert response["statusCode"] == 200
    payload = json.loads(response["body"])
    assert payload["same"] is False
    assert "+  url: https://example.test/hook2" in payload["diff"]


def test_admin_diff_is_a_viewer_route_like_the_versions_read():
    assert roles.minimum_for_route("GET", DIFF_PATH) == "viewer"
    assert roles.minimum_for_action("workflow.versions") == "viewer"


def test_admin_diff_requires_a_session(monkeypatch):
    monkeypatch.setattr(session, "authenticated", lambda event: False)
    response = admin.route(admin_request("GET", DIFF_PATH), "GET", DIFF_PATH)
    assert response["statusCode"] == 401


# ---- Agent (CLI) surface ----


def agent_request(method, path, token="dtc-token", query=None):
    request = {
        "requestContext": {"http": {"method": method, "path": path}},
        "headers": {"host": "dapier.example.test"},
        "cookies": [],
        "queryStringParameters": query or {},
    }
    if token is not None:
        request["headers"]["authorization"] = f"Bearer {token}"
    return request


AGENT_DIFF_PATH = "/api/agent/designer/workflows/test-flow.yaml/versions/diff"


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


def test_agent_diff_drives_the_same_store(two_revisions, agent_identity):
    response = agent_api.route(
        agent_request("GET", AGENT_DIFF_PATH, query={"from": "1", "to": "2"}),
        "GET", AGENT_DIFF_PATH,
    )
    assert response["statusCode"] == 200
    payload = json.loads(response["body"])
    assert payload["file"] == "test-flow.yaml"
    assert payload["same"] is False
    assert "+  url: https://example.test/hook2" in payload["diff"]
    # Like the versions read, the read itself is not audited (denials are).
    assert agent_identity == []


def test_agent_diff_gates_on_the_versions_grant(two_revisions, agent_identity):
    denied = agent_api.route(
        agent_request("GET", AGENT_DIFF_PATH.replace("test-flow", "ghost"),
                      query={"from": "1", "to": "2"}),
        "GET", AGENT_DIFF_PATH.replace("test-flow", "ghost"),
    )
    assert denied["statusCode"] == 404
    payload = json.loads(denied["body"])
    assert "no version 1 or version 2 of workflow ghost" in payload["error"]


def test_agent_diff_requires_a_bearer_identity(agent_identity):
    response = agent_api.route(
        agent_request("GET", AGENT_DIFF_PATH, token=None, query={"from": "1", "to": "2"}),
        "GET", AGENT_DIFF_PATH,
    )
    assert response["statusCode"] == 401


# ---- CLI wrapper ----

from dapier_cli import commands as cli_commands
from dapier_cli import main as cli_main
from dapier_cli.api import ApiError


def test_cli_workflows_diff_prints_the_unified_diff(monkeypatch, capsys):
    calls = []

    def fake_call(api_url, method, path, body=None, **kwargs):
        calls.append((method, path))
        return {"file": "test-flow.yaml", "same": False, "truncated": False,
                "diff": "--- test-flow.yaml v1\n+++ test-flow.yaml v2\n@@ -3 +3 @@\n-url: a\n+url: b\n"}

    monkeypatch.setattr(cli_commands.api, "call", fake_call)

    assert cli_main.main(["workflows", "diff", "test-flow.yaml",
                          "--from", "1", "--to", "2"]) == 0
    assert calls == [("GET", AGENT_DIFF_PATH + "?from=1&to=2")]
    out = capsys.readouterr().out
    assert "--- test-flow.yaml v1" in out and "+url: b" in out


def test_cli_workflows_diff_reports_identical_versions(monkeypatch, capsys):
    monkeypatch.setattr(cli_commands.api, "call",
                        lambda *args, **kwargs: {"same": True, "diff": ""})
    assert cli_main.main(["workflows", "diff", "test-flow.yaml",
                          "--from", "3", "--to", "3"]) == 0
    assert "v3 and v3 are identical." in capsys.readouterr().out


def test_cli_workflows_diff_warns_on_truncation(monkeypatch, capsys):
    monkeypatch.setattr(cli_commands.api, "call",
                        lambda *args, **kwargs: {"same": False, "truncated": True,
                                                 "diff": "-a\n+b\n"})
    assert cli_main.main(["workflows", "diff", "test-flow.yaml",
                          "--from", "1", "--to", "2"]) == 0
    _, err = capsys.readouterr()
    assert "truncated" in err


def test_cli_workflows_diff_lets_the_api_error_surface(monkeypatch, capsys):
    def fake_call(*args, **kwargs):
        raise ApiError("no such workflow: ghost.yaml", status=404)

    monkeypatch.setattr(cli_commands.api, "call", fake_call)
    assert cli_main.main(["workflows", "diff", "ghost.yaml",
                          "--from", "1", "--to", "2"]) == 4
    assert "Error: no such workflow" in capsys.readouterr().out


def test_cli_workflows_diff_requires_both_revisions(capsys):
    with pytest.raises(SystemExit):
        cli_main.main(["workflows", "diff", "test-flow.yaml", "--from", "1"])
    assert "--to" in capsys.readouterr().err


# ---- Surface parity pins (console dialog, CLI command, both routes) ----


def test_console_versions_dialog_wires_the_diff_button():
    source = (WEB / "js" / "views" / "overview.js").read_text()
    assert "/versions/diff?from=" in source, \
        "the Versions dialog's Diff button must call the versions diff endpoint"
    assert "version-diff-output" in source, "the diff renders in a <pre>"
    assert "version-diff-from" in source and "version-diff-to" in source, \
        "the dialog picks the two revisions"


def test_both_dispatchers_expose_the_diff_route():
    agent_source = (Path(__file__).resolve().parents[1]
                    / "src" / "dapier" / "api" / "agent.py").read_text()
    assert "versions/diff" in agent_source
    admin_source = (Path(__file__).resolve().parents[1]
                    / "src" / "dapier" / "api" / "admin" / "__init__.py").read_text()
    assert "versions/diff" in admin_source
    cli_source = (Path(__file__).resolve().parents[1]
                  / "dapier_cli" / "main.py").read_text()
    assert '"diff"' in cli_source
