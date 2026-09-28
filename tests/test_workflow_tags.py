"""Workflow organization (gap-analysis finding 8): tags and bulk toggle.

Tags live as a top-level ``tags:`` list in the workflow YAML — validated at
save time, edited through the tags endpoint (replace or add/remove merge),
surfaced in every list summary, and filterable with ``?tag=`` on both list
routes and the overview. Bulk enable/disable drives the same toggle semantics
per workflow, scoped by explicit ids, a tag, the search text, or all.
"""

import json

import pytest

from src.dapier.api import admin
from src.dapier.api import agent as agent_api
from src.dapier.api import designer_store
from src.dapier.api import overview as overview_api
from src.dapier.api.designer_store import WorkflowError
from src.dapier.auth import session
from src.dapier.triggers import published_workflows

# The real bulk domain function, for tests that stub api_bulk then unstub it.
REAL_API_BULK = designer_store.api_bulk

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

INVOICE_YAML = """\
id: invoice-alert
description: Alert on new invoices
trigger:
  connector: email
  event: message.received
actions:
  - id: post
    type: slack
    channel: '#ops'
"""


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
    monkeypatch.setenv(published_workflows.TABLE_ENV, "published-test")
    table = StubPublishedTable()
    monkeypatch.setattr(published_workflows, "get_table", lambda table_ref=None: table)
    return table


@pytest.fixture
def git_ready(monkeypatch):
    """Git sync pointed at a scripted GitHub; commits succeed silently."""
    monkeypatch.setenv(designer_store.REPO_URL_ENV, "https://github.com/owner/repo")
    monkeypatch.delenv(designer_store.BRANCH_ENV, raising=False)
    monkeypatch.delenv(designer_store.TOKEN_SECRET_ENV, raising=False)
    monkeypatch.setattr(designer_store, "get_token", lambda: "test-token")
    monkeypatch.setattr(designer_store, "_github",
                        lambda method, path, token, payload=None: {
                            ("GET", "/repos/owner/repo/git/ref/heads/main"):
                                {"object": {"sha": "base123"}},
                            ("GET", "/repos/owner/repo/git/commits/base123"):
                                {"tree": {"sha": "treesh"}},
                            ("POST", "/repos/owner/repo/git/trees"): {"sha": "newtree"},
                            ("POST", "/repos/owner/repo/git/commits"):
                                {"sha": "commit456", "html_url": "https://x.test/c"},
                            ("PATCH", "/repos/owner/repo/git/refs/heads/main"): {},
                        }[(method, path)])


@pytest.fixture
def bundle(monkeypatch, tmp_path):
    """A deployed bundle with two workflows: one tagged billing, one ops."""
    monkeypatch.setenv("WORKFLOWS_DIR", str(tmp_path))
    (tmp_path / "test-flow.yaml").write_text(
        WORKFLOW_YAML.replace("id: test-flow", "id: test-flow\ntags: [billing, invoices]"))
    (tmp_path / "invoice-alert.yaml").write_text(
        INVOICE_YAML.replace("id: invoice-alert", "id: invoice-alert\ntags: [OPS]"))
    return tmp_path


# ---- Save-time tag validation ----


@pytest.mark.parametrize("tags", [
    ["billing"], ["Billing", "OPS"], [" spaced "],
])
def test_parse_workflow_accepts_clean_tag_lists(tags):
    workflow = designer_store.parse_workflow(
        "id: x\n"
        f"tags: {json.dumps(tags)}\n"
        "trigger: {connector: email, event: e}\n"
        "actions: [{type: slack, channel: '#ops'}]\n")
    assert workflow["tags"] == tags


@pytest.mark.parametrize("yaml_text,fragment", [
    ("tags: billing\n", "must be a list"),                       # a bare string
    ("tags: [1, 2]\n", "must be a list"),                        # non-strings
    ("tags: ['']\n", "non-empty"),                               # blank tag
    (f"tags: [{', '.join(['a'] * (designer_store.MAX_TAGS + 1))}]\n", "at most"),
    (f"tags: ['{'x' * (designer_store.MAX_TAG_LENGTH + 1)}']\n", "at most"),
])
def test_parse_workflow_rejects_bad_tag_lists(yaml_text, fragment):
    with pytest.raises(WorkflowError, match=fragment):
        designer_store.parse_workflow(
            "id: x\n"
            + yaml_text
            + "trigger: {connector: email, event: e}\n"
              "actions: [{type: slack, channel: '#ops'}]\n")


# ---- List summaries and the ?tag= / ?q= filters ----


def test_summaries_carry_tags_and_the_tag_filter_narrows(bundle):
    _, payload = designer_store.api_list()
    by_id = {row["id"]: row for row in payload["workflows"]}
    assert by_id["test-flow"]["tags"] == ["billing", "invoices"]
    assert by_id["invoice-alert"]["tags"] == ["OPS"]

    _, tagged = designer_store.api_list(tag="BILLING")  # case-insensitive
    assert [row["id"] for row in tagged["workflows"]] == ["test-flow"]
    _, searched = designer_store.api_list(q="ops")  # search covers tags too
    assert [row["id"] for row in searched["workflows"]] == ["invoice-alert"]
    _, none = designer_store.api_list(tag="nope")
    assert none["workflows"] == []


def test_designer_list_route_passes_tag_to_the_store(monkeypatch, operator_session,
                                                     agent_operator):
    seen = {}
    monkeypatch.setattr(designer_store, "api_list",
                        lambda q=None, tag=None, folder=None:
                        seen.update(q=q, tag=tag) or (200, {"workflows": [], "git_sync": {}}))
    event = admin_request("GET", "/api/admin/designer/workflows")
    event["queryStringParameters"] = {"tag": "billing"}
    admin.route(event, "GET", "/api/admin/designer/workflows")
    assert seen["tag"] == "billing"

    event = agent_request("GET", "/api/agent/designer/workflows")
    event["queryStringParameters"] = {"tag": "ops"}
    agent_api.route(event, "GET", "/api/agent/designer/workflows")
    assert seen["tag"] == "ops"


# ---- Domain: designer_store.api_tags ----


def test_api_tags_replaces_normalizes_and_dedupes(published, git_ready, bundle):
    status, payload = designer_store.api_tags(
        "test-flow.yaml", {"tags": ["Billing", "ops", "billing"]}, operator="op-1")

    assert status == 200
    assert payload["tags"] == ["billing", "ops"]  # lowercase, deduped, sorted
    assert payload["published"] is True
    live = published_workflows.get_item("test-flow")
    assert live["workflow"]["tags"] == ["billing", "ops"]  # stored in the YAML
    assert live["workflow"]["actions"][0]["url"] == "https://example.test/hook"


def test_api_tags_add_remove_merge(published, git_ready, bundle):
    assert designer_store.api_tags("test-flow.yaml", {"tags": ["billing"]})[0] == 200

    status, payload = designer_store.api_tags(
        "test-flow.yaml", {"add": ["Ops", "daily"], "remove": ["BILLING"]})

    assert status == 200
    # Removal wins, case-insensitively; adds land lowercase.
    assert payload["tags"] == ["daily", "ops"]


def test_api_tags_add_remove_reports_failures_without_silence(published, git_ready, bundle):
    status, payload = designer_store.api_tags("test-flow.yaml", {"add": [7]})
    assert status == 400
    assert "list of strings" in payload["error"]

    status, payload = designer_store.api_tags(
        "test-flow.yaml", {"add": ["x" * (designer_store.MAX_TAG_LENGTH + 1)]})
    assert status == 400
    assert "at most" in payload["error"]

    status, payload = designer_store.api_tags(
        "test-flow.yaml",
        {"add": [str(index) for index in range(designer_store.MAX_TAGS)]})
    assert status == 400
    assert "at most" in payload["error"]


def test_api_tags_clear_removes_the_key(published, git_ready, bundle):
    assert designer_store.api_tags("test-flow.yaml", {"tags": ["billing"]})[0] == 200

    status, payload = designer_store.api_tags("test-flow.yaml", {"tags": []})

    assert status == 200
    assert payload["tags"] == []
    assert "tags" not in published_workflows.get_item("test-flow")["workflow"]


def test_api_tags_body_validation(published, git_ready, bundle):
    assert designer_store.api_tags("test-flow.yaml", {})[0] == 400
    assert designer_store.api_tags("test-flow.yaml", {"tags": ["a"], "add": ["b"]})[0] == 400
    assert designer_store.api_tags("test-flow.yaml", {"tags": "billing"})[0] == 400
    assert designer_store.api_tags("test-flow.yaml", {"add": "billing"})[0] == 400
    assert designer_store.api_tags("nope.yaml", {"tags": ["a"]})[0] == 404
    assert designer_store.api_tags("../etc", {"tags": ["a"]})[0] == 400


def test_api_tags_without_the_publish_table_is_503(monkeypatch, bundle):
    monkeypatch.delenv(published_workflows.TABLE_ENV, raising=False)
    assert designer_store.api_tags("test-flow.yaml", {"tags": ["a"]})[0] == 503


def test_api_tags_records_a_tags_cause_in_version_history(published, git_ready, bundle):
    assert designer_store.api_save({"yaml": WORKFLOW_YAML}, operator="op-1")[0] == 200
    assert designer_store.api_tags("test-flow.yaml", {"tags": ["billing"]}, operator="op-2")

    versions = published_workflows.list_versions("test-flow")
    assert versions[0]["cause"] == "tags"
    assert versions[0]["published_by"] == "op-2"


# ---- Domain: designer_store.api_bulk ----


def test_api_bulk_ids_toggles_each_and_reports_per_id(published, git_ready, bundle):
    status, payload = designer_store.api_bulk(
        {"ids": ["test-flow.yaml", "invoice-alert.yaml"], "action": "disable"})

    assert status == 200
    assert payload["action"] == "disable"
    assert payload["requested"] == 2
    assert payload["ok"] == 2
    assert all(result["ok"] for result in payload["results"])
    assert published_workflows.get_item("test-flow")["enabled"] is False
    assert published_workflows.get_item("invoice-alert")["enabled"] is False


def test_api_bulk_unknown_id_fails_alone(published, git_ready, bundle):
    status, payload = designer_store.api_bulk(
        {"ids": ["test-flow.yaml", "nope.yaml"], "action": "enable"})

    assert status == 200  # the batch answers; the failure is per id
    results = {result["id"]: result for result in payload["results"]}
    assert results["test-flow.yaml"]["ok"] is True
    assert results["nope.yaml"]["ok"] is False
    assert "no such workflow" in results["nope.yaml"]["error"]


def test_api_bulk_tag_scope_toggles_every_tagged_workflow(published, git_ready, bundle):
    status, payload = designer_store.api_bulk({"action": "disable", "tag": "billing"})

    assert status == 200
    assert payload["scope"] == "tag:billing"
    assert payload["requested"] == 1
    assert payload["results"][0]["id"] == "test-flow"
    assert published_workflows.get_item("test-flow")["enabled"] is False
    assert published_workflows.get_item("invoice-alert") is None  # untouched


def test_api_bulk_search_scope_uses_the_list_match(published, git_ready, bundle):
    status, payload = designer_store.api_bulk({"action": "enable", "search": "alert"})

    assert status == 200
    assert payload["scope"] == "search:alert"
    assert [result["id"] for result in payload["results"]] == ["invoice-alert"]


def test_api_bulk_all_scope_hits_every_workflow(published, git_ready, bundle):
    status, payload = designer_store.api_bulk({"action": "disable", "all": True})

    assert status == 200
    assert payload["scope"] == "all"
    assert payload["requested"] == 2
    assert published_workflows.get_item("test-flow")["enabled"] is False
    assert published_workflows.get_item("invoice-alert")["enabled"] is False


def test_api_bulk_validation_refuses_scopes_it_cannot_answer(published, bundle):
    assert designer_store.api_bulk({"action": "pause"})[0] == 400  # bad action
    assert designer_store.api_bulk({})[0] == 400  # no scope
    assert designer_store.api_bulk({"action": "enable"})[0] == 400  # still no scope
    assert designer_store.api_bulk(
        {"action": "enable", "tag": "a", "search": "b"})[0] == 400  # two scopes
    assert designer_store.api_bulk(
        {"action": "enable", "ids": ["a.yaml"], "all": True})[0] == 400
    assert designer_store.api_bulk({"action": "enable", "ids": "a.yaml"})[0] == 400
    assert designer_store.api_bulk({"action": "enable", "tag": 7})[0] == 400
    assert designer_store.api_bulk(
        {"action": "enable", "ids": ["a.yaml"] * (designer_store.MAX_BULK_IDS + 1)})[0] == 400
    assert designer_store.api_bulk({"action": "enable", "ids": []})[0] == 400
    assert designer_store.api_bulk("not an object")[0] == 400


# ---- Routes ----


def admin_request(method, path, body=None, query=None):
    request = {
        "requestContext": {"http": {"method": method, "path": path}},
        "headers": {"host": "dapier.example.test"},
        "cookies": [],
        "body": json.dumps(body) if body is not None else None,
    }
    if query is not None:
        request["queryStringParameters"] = query
    return request


def agent_request(method, path, body=None, token="dtc-token", query=None):
    request = {
        "requestContext": {"http": {"method": method, "path": path}},
        "headers": {"host": "dapier.example.test"},
        "cookies": [],
    }
    if token is not None:
        request["headers"]["authorization"] = f"Bearer {token}"
    if body is not None:
        request["body"] = json.dumps(body)
    if query is not None:
        request["queryStringParameters"] = query
    return request


@pytest.fixture
def operator_session(monkeypatch):
    monkeypatch.setattr(session, "authenticated", lambda event: True)
    monkeypatch.setattr(session, "_csrf_ok", lambda event, method: True)
    monkeypatch.setattr(session, "require_operator", lambda event: ({"sub": "op-1"}, None))
    # Roles v1: admin.route gates through require_role; handlers still call
    # require_operator, so both answer with the same operator payload.
    monkeypatch.setattr(session, "require_role",
                        lambda event, minimum="operator": ({"sub": "op-1"}, None))
    monkeypatch.setattr(session, "_audit_event", lambda *args, **kwargs: None)


def _agent_bearer(monkeypatch, *, operator):
    monkeypatch.delenv("AUDIT_TABLE", raising=False)
    monkeypatch.delenv("OPERATOR_SUBJECTS", raising=False)
    if operator:
        monkeypatch.setenv("OPERATOR_EMAILS", "op@datatalks.club")
    else:
        monkeypatch.setenv("OPERATOR_EMAILS", "someone-else@datatalks.club")
    monkeypatch.setenv("AUTH_CLI_CLIENT_ID", "cli-client")
    monkeypatch.setattr(
        agent_api, "verify_id_token",
        lambda token, audience=None: {"sub": "agent-op", "email": "op@datatalks.club"},
    )
    monkeypatch.setattr(agent_api, "audit", type("Audit", (), {
        "emit": staticmethod(lambda *args, **kwargs: None),
    }))


@pytest.fixture
def agent_operator(monkeypatch):
    _agent_bearer(monkeypatch, operator=True)


@pytest.fixture
def agent_non_operator(monkeypatch):
    _agent_bearer(monkeypatch, operator=False)


def test_admin_tags_route_drives_the_store_and_audits(monkeypatch, operator_session):
    monkeypatch.setattr(designer_store, "api_tags",
                        lambda source, body, operator=None:
                        (200, {"file": source, "tags": body.get("tags", [])}))
    audits = []
    monkeypatch.setattr(session, "_audit_event",
                        lambda *args, **kwargs: audits.append((args, kwargs)) or None)

    response = admin.route(
        admin_request("PUT", "/api/admin/designer/workflows/test-flow.yaml/tags",
                      {"tags": ["billing"]}),
        "PUT", "/api/admin/designer/workflows/test-flow.yaml/tags",
    )

    assert response["statusCode"] == 200
    assert audits[0][0][1] == "workflow.tags"


def test_admin_bulk_route_drives_the_store_and_audits(monkeypatch, operator_session):
    monkeypatch.setattr(designer_store, "api_bulk",
                        lambda body, operator=None:
                        (200, {"action": body.get("action"), "requested": 1, "ok": 1,
                               "results": [{"id": "test-flow.yaml", "ok": True}]}))
    audits = []
    monkeypatch.setattr(session, "_audit_event",
                        lambda *args, **kwargs: audits.append((args, kwargs)) or None)

    response = admin.route(
        admin_request("POST", "/api/admin/designer/workflows/bulk",
                      {"ids": ["test-flow.yaml"], "action": "disable"}),
        "POST", "/api/admin/designer/workflows/bulk",
    )

    assert response["statusCode"] == 200
    assert audits[0][0][1] == "workflow.bulk-toggle"
    assert "test-flow.yaml" in audits[0][0][0]  # the batch subject names the ids

    # Without the stub, the store's own validation answers a scopeless body.
    monkeypatch.setattr(designer_store, "api_bulk", REAL_API_BULK)
    bad = admin.route(
        admin_request("POST", "/api/admin/designer/workflows/bulk", {"ids": ["x.yaml"]}),
        "POST", "/api/admin/designer/workflows/bulk",
    )
    assert bad["statusCode"] == 400


def test_agent_tags_route_is_operator_gated_and_drives_the_store(monkeypatch, bundle):
    _agent_bearer(monkeypatch, operator=True)
    calls = []
    monkeypatch.setattr(designer_store, "api_tags",
                        lambda source, body, operator=None:
                        calls.append((source, body, operator))
                        or (200, {"file": source, "tags": body.get("tags", [])}))

    response = agent_api.route(
        agent_request("PUT", "/api/agent/designer/workflows/test-flow.yaml/tags",
                      {"tags": ["billing"]}),
        "PUT", "/api/agent/designer/workflows/test-flow.yaml/tags",
    )
    assert response["statusCode"] == 200
    assert calls == [("test-flow.yaml", {"tags": ["billing"]}, "agent-op")]


def test_agent_bulk_route_is_operator_gated_and_drives_the_store(monkeypatch, bundle):
    _agent_bearer(monkeypatch, operator=True)
    calls = []
    monkeypatch.setattr(designer_store, "api_bulk",
                        lambda body, operator=None:
                        calls.append((body, operator))
                        or (200, {"action": body["action"], "requested": 0, "ok": 0,
                                  "results": []}))

    response = agent_api.route(
        agent_request("POST", "/api/agent/designer/workflows/bulk",
                      {"tag": "billing", "action": "disable"}),
        "POST", "/api/agent/designer/workflows/bulk",
    )
    assert response["statusCode"] == 200
    assert calls == [({"tag": "billing", "action": "disable"}, "agent-op")]

    # A machine API token or a non-operator account is denied.
    _agent_bearer(monkeypatch, operator=False)
    denied = agent_api.route(
        agent_request("POST", "/api/agent/designer/workflows/bulk",
                      {"tag": "billing", "action": "disable"}),
        "POST", "/api/agent/designer/workflows/bulk",
    )
    assert denied["statusCode"] == 403


# ---- Overview: tags on each row, ?tag= filter, and the aggregate ----


def test_overview_view_carries_tags():
    view = overview_api._workflow_view(
        {"id": "test-flow", "tags": ["billing", "invoices", "", 7],
         "trigger": {"connector": "email", "event": "message.received"}},
        "test-flow.yaml", published=False,
    )
    assert view["tags"] == ["billing", "invoices"]


def test_overview_tag_filter_and_aggregate(monkeypatch):
    views = [
        {"id": "test-flow", "description": "", "enabled": True,
         "trigger": {"connector": "email", "event": "message.received"},
         "triggerCount": 1, "actions": [{"type": "slack"}], "published": False,
         "tags": ["billing", "invoices"]},
        {"id": "invoice-alert", "description": "", "enabled": True,
         "trigger": {"connector": "schedule", "event": "tick"},
         "triggerCount": 1, "actions": [{"type": "dropbox_upload"}], "published": False,
         "tags": ["ops"]},
    ]
    monkeypatch.setattr(overview_api, "_workflows", lambda: views)
    monkeypatch.setattr(overview_api, "_scan", lambda *args, **kwargs: [])
    monkeypatch.setenv("EXECUTIONS_TABLE", "executions")
    monkeypatch.setenv("CONNECTIONS_TABLE", "connections")
    monkeypatch.setattr(overview_api, "_credential_status",
                        lambda provider: {"provider": provider})
    monkeypatch.setattr(overview_api, "_oauth_client_status", lambda provider: {"provider": provider})
    monkeypatch.setattr(overview_api.api_tokens, "list_all", lambda: [])
    monkeypatch.setattr(overview_api, "_email_triggers",
                        lambda: {"domain": "", "triggers": [], "yaml_routes": []})
    monkeypatch.setattr(overview_api.runs, "recent", lambda *args, **kwargs: [])
    monkeypatch.delenv("TASK_USAGE_TABLE", raising=False)

    event = admin_request("GET", "/api/admin/overview")
    event["queryStringParameters"] = {"tag": "OPS"}  # case-insensitive
    payload = json.loads(overview_api.overview(event)["body"])
    assert [workflow["id"] for workflow in payload["workflows"]] == ["invoice-alert"]

    unfiltered = json.loads(overview_api.overview(admin_request("GET", "/api/admin/overview"))["body"])
    assert unfiltered["workflow_tags"] == ["billing", "invoices", "ops"]


# ---- CLI ----


from dapier_cli import commands as cli_commands  # noqa: E402
from dapier_cli import config, main as cli_main  # noqa: E402


@pytest.fixture
def isolated_home(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.delenv("DAPIER_API_URL", raising=False)
    config.save_session({"id_token": "abc", "subject": "s", "email": "e",
                         "expires_at": 9_999_999_999})
    return tmp_path


def test_cli_tags_replace_clear_add_remove(isolated_home, monkeypatch, capsys):
    seen = []

    def fake_call(api_url, method, path, body=None, **kwargs):
        seen.append((method, path, body))
        return {"file": "test-flow.yaml", "tags": ["billing", "ops"], "published": True}

    monkeypatch.setattr(cli_commands.api, "call", fake_call)

    assert cli_main.main(["workflows", "tags", "test-flow.yaml", "--tags", "Billing, ops"]) == 0
    assert seen[-1] == ("PUT", "/api/agent/designer/workflows/test-flow.yaml/tags",
                        {"tags": ["Billing", "ops"]})
    assert "billing, ops" in capsys.readouterr().out

    assert cli_main.main(["workflows", "tags", "test-flow.yaml", "--clear"]) == 0
    assert seen[-1][2] == {"tags": []}

    assert cli_main.main(
        ["workflows", "tags", "test-flow.yaml", "--add", "daily", "--remove", "billing"]) == 0
    assert seen[-1][2] == {"add": ["daily"], "remove": ["billing"]}

    # No flags: nothing to do, no call.
    assert cli_main.main(["workflows", "tags", "test-flow.yaml"]) == 2
    assert len(seen) == 3


def test_cli_bulk_enable_disable_by_tag_and_all(isolated_home, monkeypatch, capsys):
    seen = []

    def fake_call(api_url, method, path, body=None, **kwargs):
        seen.append((method, path, body))
        return {"action": body["action"], "scope": "tag:billing" if body.get("tag") else "all",
                "requested": 2, "ok": 2,
                "results": [{"id": "test-flow", "ok": True},
                            {"id": "broken", "ok": False, "error": "no such workflow"}]}

    monkeypatch.setattr(cli_commands.api, "call", fake_call)

    assert cli_main.main(["workflows", "disable", "--tag", "billing"]) == 0
    assert seen[-1] == ("POST", "/api/agent/designer/workflows/bulk",
                        {"action": "disable", "tag": "billing"})
    out = capsys.readouterr().out
    assert "2 of 2" in out and "tag:billing" in out
    assert "broken" in out  # per-workflow failures are printed, never silent

    assert cli_main.main(["workflows", "enable", "--all"]) == 0
    assert seen[-1][2] == {"action": "enable", "all": True}

    # Explicit files still go through the ids scope.
    assert cli_main.main(["workflows", "on", "a.yaml", "b.yaml"]) == 0
    assert seen[-1][2] == {"ids": ["a.yaml", "b.yaml"], "action": "enable"}

    # Neither files nor a scope: refused loudly, no call.
    assert cli_main.main(["workflows", "enable"]) == 2
    assert len(seen) == 3


def test_cli_list_forwards_the_tag_filter(isolated_home, monkeypatch, capsys):
    calls = []

    def fake_call(api_url, method, path, body=None, **kwargs):
        calls.append((method, path))
        return {"workflows": [{"id": "test-flow", "enabled": True,
                               "source": "test-flow.yaml", "connector": "email",
                               "event": "message.received", "actionCount": 1,
                               "tags": ["billing"]}],
                "git_sync": {"configured": False, "repo": "r", "branch": "main"}}

    monkeypatch.setattr(cli_commands.api, "call", fake_call)

    assert cli_main.main(["workflows", "list", "--tag", "billing"]) == 0
    assert calls == [("GET", "/api/agent/designer/workflows?tag=billing")]
    assert "[billing]" in capsys.readouterr().out
