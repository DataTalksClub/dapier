import base64

import pytest

from src.dapier.api import designer_store

WORKFLOW_YAML = """\
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


@pytest.fixture
def github_env(monkeypatch):
    monkeypatch.delenv(designer_store.TOKEN_SECRET_ENV, raising=False)
    monkeypatch.setenv(designer_store.REPO_URL_ENV, "https://github.com/owner/repo")
    monkeypatch.delenv(designer_store.BRANCH_ENV, raising=False)


@pytest.fixture
def github_calls(monkeypatch):
    calls = []

    def fake(method, path, token, payload=None):
        calls.append((method, path, token, payload))
        return GITHUB_SCRIPT[(method, path)]

    monkeypatch.setattr(designer_store, "_github", fake)
    return calls


def test_parse_workflow_accepts_known_and_unknown_actions():
    workflow = designer_store.parse_workflow(WORKFLOW_YAML)
    assert workflow["id"] == "test-flow"
    assert [action["type"] for action in workflow["actions"]] == ["webhook", "mystery_action"]


@pytest.mark.parametrize("yaml_text,fragment", [
    ("", "required"),
    ("- just\n- a list\n", "YAML object"),
    ("id: [1]\n", "needs an id"),
    ("trigger: {connector: email}\n", "needs an id"),
    ("id: Bad/Slash\n", "needs an id"),
    ("id: x\ntrigger: {connector: email}\nactions: [{id: a, type: webhook}]\n", "connector and an event"),
    ("id: x\nactions: [{id: a, type: webhook}]\n", "needs a trigger"),
    ("id: x\ntrigger: {connector: email, event: e}\n", "at least one action"),
    ("id: x\ntrigger: {connector: email, event: e}\nactions: [just-a-string]\n", "needs a type"),
    ("id: x\ntrigger: {connector: email, event: e}\nactions: [{type: ''}]\n", "needs a type"),
    ("id: x\ntrigger: {connector: [nope, event: e}\nactions: []\n", "invalid YAML"),
])
def test_parse_workflow_rejects_invalid_definitions(yaml_text, fragment):
    with pytest.raises(designer_store.WorkflowError, match=fragment):
        designer_store.parse_workflow(yaml_text)


def test_parse_workflow_rejects_oversized_yaml():
    with pytest.raises(designer_store.WorkflowError, match="too large"):
        designer_store.parse_workflow("id: x\n# " + "x" * (designer_store.MAX_YAML_BYTES + 10))


def test_filename_for_derives_yaml_name():
    assert designer_store.filename_for("render-invoice") == "render-invoice.yaml"


@pytest.mark.usefixtures("github_env")
def test_repo_slug_parses_repo_url(monkeypatch):
    assert designer_store.repo_slug() == "owner/repo"
    monkeypatch.setenv(designer_store.REPO_URL_ENV, "git@github.com:DataTalksClub/dapier.git")
    assert designer_store.repo_slug() == "DataTalksClub/dapier"
    monkeypatch.setenv(designer_store.REPO_URL_ENV, "not-a-github-url")
    with pytest.raises(designer_store.SyncConfigError):
        designer_store.repo_slug()


@pytest.mark.usefixtures("github_env")
def test_sync_status_reflects_token_configuration(monkeypatch):
    assert designer_store.sync_status() == {
        "git_sync": {"configured": False, "repo": "owner/repo", "branch": "main"},
    }
    monkeypatch.setenv(designer_store.TOKEN_SECRET_ENV, "dapier/designer/token")
    monkeypatch.setenv(designer_store.BRANCH_ENV, "main")
    assert designer_store.sync_status()["git_sync"] == {
        "configured": True, "repo": "owner/repo", "branch": "main",
    }


@pytest.mark.usefixtures("github_env")
def test_get_token_requires_configured_secret(monkeypatch):
    with pytest.raises(designer_store.SyncConfigError, match="not configured"):
        designer_store.get_token()


@pytest.mark.usefixtures("github_env")
def test_commit_workflow_builds_one_atomic_commit(github_calls):
    result = designer_store.commit_workflow(WORKFLOW_YAML, message="designer: save workflow test-flow", token="tok")

    assert result == {
        "file": "test-flow.yaml",
        "removed": None,
        "commit": "commit456",
        "html_url": "https://github.com/owner/repo/commit/commit456",
    }
    methods = [(method, path) for method, path, _, _ in github_calls]
    assert methods == [
        ("GET", "/repos/owner/repo/git/ref/heads/main"),
        ("GET", "/repos/owner/repo/git/commits/base123"),
        ("POST", "/repos/owner/repo/git/trees"),
        ("POST", "/repos/owner/repo/git/commits"),
        ("PATCH", "/repos/owner/repo/git/refs/heads/main"),
    ]
    assert github_calls[0][2] == "tok"
    tree_payload = github_calls[2][3]
    assert tree_payload["base_tree"] == "treesh"
    assert tree_payload["tree"] == [{
        "path": "workflows/test-flow.yaml",
        "mode": "100644",
        "type": "blob",
        "content": WORKFLOW_YAML,
    }]
    commit_payload = github_calls[3][3]
    assert commit_payload == {
        "message": "designer: save workflow test-flow",
        "tree": "newtree",
        "parents": ["base123"],
    }
    assert github_calls[4][3] == {"sha": "commit456"}


@pytest.mark.usefixtures("github_env")
def test_commit_workflow_rename_deletes_old_file_in_same_commit(github_calls):
    result = designer_store.commit_workflow(
        WORKFLOW_YAML, message="designer: save workflow test-flow",
        rename_from="old-name.yaml", token="tok",
    )

    assert result["removed"] == "old-name.yaml"
    tree = github_calls[2][3]["tree"]
    assert tree[0]["path"] == "workflows/test-flow.yaml"
    assert tree[1] == {"path": "workflows/old-name.yaml", "mode": "100644", "type": "blob", "sha": None}


@pytest.mark.usefixtures("github_env")
def test_commit_workflow_same_name_rename_skips_delete(github_calls):
    designer_store.commit_workflow(
        WORKFLOW_YAML, message="save", rename_from="test-flow.yaml", token="tok",
    )
    assert len(github_calls[2][3]["tree"]) == 1


@pytest.mark.usefixtures("github_env")
def test_commit_workflow_validates_before_touching_github(github_calls):
    with pytest.raises(designer_store.WorkflowError, match="needs an id"):
        designer_store.commit_workflow("actions: []\n", message="save", token="tok")
    with pytest.raises(designer_store.WorkflowError, match="invalid source file"):
        designer_store.commit_workflow(WORKFLOW_YAML, message="save", rename_from="../x.yaml", token="tok")
    assert github_calls == []


@pytest.mark.usefixtures("github_env")
def test_fetch_workflow_decodes_content(monkeypatch):
    encoded = base64.b64encode(WORKFLOW_YAML.encode()).decode()
    seen = {}

    def stub(method, path, token, payload=None):
        seen["path"] = path
        return {"encoding": "base64", "content": encoded}

    monkeypatch.setattr(designer_store, "_github", stub)
    workflow = designer_store.fetch_workflow("test-flow.yaml", token="tok")
    assert seen["path"] == "/repos/owner/repo/contents/workflows/test-flow.yaml?ref=main"
    assert workflow["id"] == "test-flow"


@pytest.mark.usefixtures("github_env")
def test_fetch_workflow_rejects_unexpected_payload(monkeypatch):
    monkeypatch.setattr(
        designer_store, "_github",
        lambda method, path, token, payload=None: {"encoding": "none", "content": ""},
    )
    with pytest.raises(designer_store.SyncError, match="unexpected payload"):
        designer_store.fetch_workflow("test-flow.yaml", token="tok")


@pytest.mark.usefixtures("github_env")
def test_fetch_workflow_maps_404_to_keyerror(monkeypatch):
    def not_found(method, path, token, payload=None):
        raise designer_store.SyncError("github refused GET x: HTTP 404 not found")

    monkeypatch.setattr(designer_store, "_github", not_found)
    with pytest.raises(KeyError):
        designer_store.fetch_workflow("missing.yaml", token="tok")

    def server_error(method, path, token, payload=None):
        raise designer_store.SyncError("github refused GET x: HTTP 500 oops")

    monkeypatch.setattr(designer_store, "_github", server_error)
    with pytest.raises(designer_store.SyncError):
        designer_store.fetch_workflow("missing.yaml", token="tok")


# ---- Admin + agent API surface ----

import json

from src.dapier.api import admin
from src.dapier.api import agent as agent_api
from src.dapier.api import router as ingress


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
    monkeypatch.setattr(session, "_audit_event", lambda *args, **kwargs: {"emitted": (args, kwargs)})


def test_designer_list_requires_session(monkeypatch):
    monkeypatch.setattr(session, "authenticated", lambda event: False)
    response = admin.route(
        admin_request("GET", "/api/admin/designer/workflows"), "GET", "/api/admin/designer/workflows",
    )
    assert response["statusCode"] == 401


def test_designer_list_reports_bundled_workflows_and_sync(monkeypatch, tmp_path, operator_session):
    (tmp_path / "sample-flow.yaml").write_text(WORKFLOW_YAML)
    monkeypatch.setenv("WORKFLOWS_DIR", str(tmp_path))
    monkeypatch.delenv(designer_store.TOKEN_SECRET_ENV, raising=False)
    monkeypatch.setenv(designer_store.REPO_URL_ENV, "https://github.com/owner/repo")

    response = admin.route(
        admin_request("GET", "/api/admin/designer/workflows"), "GET", "/api/admin/designer/workflows",
    )
    payload = json.loads(response["body"])
    assert response["statusCode"] == 200
    assert [item["source"] for item in payload["workflows"]] == ["sample-flow.yaml"]
    assert payload["workflows"][0]["actionCount"] == 2
    assert payload["git_sync"] == {"configured": False, "repo": "owner/repo", "branch": "main"}


def test_designer_get_serves_bundled_yaml_and_404(monkeypatch, tmp_path, operator_session):
    (tmp_path / "sample-flow.yaml").write_text(WORKFLOW_YAML)
    monkeypatch.setenv("WORKFLOWS_DIR", str(tmp_path))
    monkeypatch.delenv(designer_store.TOKEN_SECRET_ENV, raising=False)

    found = admin.route(
        admin_request("GET", "/api/admin/designer/workflows/sample-flow.yaml"),
        "GET", "/api/admin/designer/workflows/sample-flow.yaml",
    )
    assert found["statusCode"] == 200
    assert json.loads(found["body"])["workflow"]["id"] == "test-flow"

    missing = admin.route(
        admin_request("GET", "/api/admin/designer/workflows/nope.yaml"),
        "GET", "/api/admin/designer/workflows/nope.yaml",
    )
    assert missing["statusCode"] == 404


def test_designer_save_commits_and_audits(monkeypatch, operator_session):
    saved = {}
    monkeypatch.setattr(designer_store, "api_save", lambda body: (
        saved.update(body) or (200, {"file": "test-flow.yaml", "commit": "abc123", "removed": None})
    ))
    audits = []
    monkeypatch.setattr(session, "_audit_event",
                        lambda *args, **kwargs: audits.append((args, kwargs)) or None)

    response = admin.route(
        admin_request("PUT", "/api/admin/designer/workflows", {"yaml": WORKFLOW_YAML}),
        "PUT", "/api/admin/designer/workflows",
    )
    assert response["statusCode"] == 200
    assert saved["yaml"] == WORKFLOW_YAML
    assert audits[0][0][1] == "workflow.save"


def test_designer_save_maps_validation_errors(monkeypatch, operator_session):
    monkeypatch.setattr(designer_store, "api_save", lambda body: (400, {"error": "workflow needs an id"}))
    response = admin.route(
        admin_request("PUT", "/api/admin/designer/workflows", {"yaml": "id: [1]\n"}),
        "PUT", "/api/admin/designer/workflows",
    )
    assert response["statusCode"] == 400
    assert "needs an id" in response["body"]


def test_designer_save_without_git_sync_reports_setup(monkeypatch, operator_session, github_env):
    response = admin.route(
        admin_request("PUT", "/api/admin/designer/workflows", {"yaml": WORKFLOW_YAML}),
        "PUT", "/api/admin/designer/workflows",
    )
    assert response["statusCode"] == 503
    assert "not configured" in response["body"]


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


def test_agent_designer_list_and_save_drive_the_same_store(monkeypatch, agent_identity):
    monkeypatch.setattr(designer_store, "api_list", lambda: (200, {"workflows": [], "git_sync": {}}))
    listed = agent_api.route(
        agent_request("GET", "/api/agent/designer/workflows"), "GET", "/api/agent/designer/workflows",
    )
    assert listed["statusCode"] == 200

    calls = []
    monkeypatch.setattr(designer_store, "api_save", lambda body: calls.append(body) or (
        200, {"file": "x.yaml", "commit": "abc", "removed": None},
    ))
    saved = agent_api.route(
        agent_request("PUT", "/api/agent/designer/workflows", {"yaml": WORKFLOW_YAML}),
        "PUT", "/api/agent/designer/workflows",
    )
    assert saved["statusCode"] == 200
    assert calls == [{"yaml": WORKFLOW_YAML}]

    monkeypatch.setattr(
        agent_api, "verify_id_token",
        lambda token, audience=None: (_ for _ in ()).throw(ValueError("bad")),
    )
    assert agent_api.route(
        agent_request("GET", "/api/agent/designer/workflows", token="expired"),
        "GET", "/api/agent/designer/workflows",
    )["statusCode"] == 401


# ---- Static serving ----

def test_designer_page_is_served_with_inline_style_csp():
    response = ingress._static("/designer")
    assert response["statusCode"] == 200
    assert response["headers"]["content-type"].startswith("text/html")
    assert "style-src 'self' 'unsafe-inline'" in response["headers"]["content-security-policy"]
    assert response["headers"]["cache-control"] == "no-store"
    assert 'id="designer-root"' in response["body"]


def test_designer_assets_are_served_cacheable():
    js = ingress._static("/assets/designer.js")
    assert js["statusCode"] == 200
    assert js["headers"]["content-type"].startswith("text/javascript")
    assert js["headers"]["cache-control"] == "public, max-age=300"
    css = ingress._static("/assets/designer.css")
    assert css["statusCode"] == 200
    assert css["headers"]["content-type"].startswith("text/css")


def test_unknown_static_path_is_not_served():
    assert ingress._static("/assets/nope.js") is None
    assert ingress._static("/designer/extra") is None


# ---- Admin + agent API surface ----

import json

from src.dapier.api import admin
from src.dapier.api import agent as agent_api
from src.dapier.api import router as ingress


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
    monkeypatch.setattr(session, "_audit_event", lambda *args, **kwargs: {"emitted": (args, kwargs)})


def test_designer_list_requires_session(monkeypatch):
    monkeypatch.setattr(session, "authenticated", lambda event: False)
    response = admin.route(
        admin_request("GET", "/api/admin/designer/workflows"), "GET", "/api/admin/designer/workflows",
    )
    assert response["statusCode"] == 401


def test_designer_list_reports_bundled_workflows_and_sync(monkeypatch, tmp_path, operator_session):
    (tmp_path / "sample-flow.yaml").write_text(WORKFLOW_YAML)
    monkeypatch.setenv("WORKFLOWS_DIR", str(tmp_path))
    monkeypatch.delenv(designer_store.TOKEN_SECRET_ENV, raising=False)
    monkeypatch.setenv(designer_store.REPO_URL_ENV, "https://github.com/owner/repo")

    response = admin.route(
        admin_request("GET", "/api/admin/designer/workflows"), "GET", "/api/admin/designer/workflows",
    )
    payload = json.loads(response["body"])
    assert response["statusCode"] == 200
    assert [item["source"] for item in payload["workflows"]] == ["sample-flow.yaml"]
    assert payload["workflows"][0]["actionCount"] == 2
    assert payload["git_sync"] == {"configured": False, "repo": "owner/repo", "branch": "main"}


def test_designer_get_serves_bundled_yaml_and_404(monkeypatch, tmp_path, operator_session):
    (tmp_path / "sample-flow.yaml").write_text(WORKFLOW_YAML)
    monkeypatch.setenv("WORKFLOWS_DIR", str(tmp_path))
    monkeypatch.delenv(designer_store.TOKEN_SECRET_ENV, raising=False)

    found = admin.route(
        admin_request("GET", "/api/admin/designer/workflows/sample-flow.yaml"),
        "GET", "/api/admin/designer/workflows/sample-flow.yaml",
    )
    assert found["statusCode"] == 200
    assert json.loads(found["body"])["workflow"]["id"] == "test-flow"

    missing = admin.route(
        admin_request("GET", "/api/admin/designer/workflows/nope.yaml"),
        "GET", "/api/admin/designer/workflows/nope.yaml",
    )
    assert missing["statusCode"] == 404


def test_designer_save_commits_and_audits(monkeypatch, operator_session):
    saved = {}
    monkeypatch.setattr(designer_store, "api_save", lambda body: (
        saved.update(body) or (200, {"file": "test-flow.yaml", "commit": "abc123", "removed": None})
    ))
    audits = []
    monkeypatch.setattr(session, "_audit_event",
                        lambda *args, **kwargs: audits.append((args, kwargs)) or None)

    response = admin.route(
        admin_request("PUT", "/api/admin/designer/workflows", {"yaml": WORKFLOW_YAML}),
        "PUT", "/api/admin/designer/workflows",
    )
    assert response["statusCode"] == 200
    assert saved["yaml"] == WORKFLOW_YAML
    assert audits[0][0][1] == "workflow.save"


def test_designer_save_maps_validation_errors(monkeypatch, operator_session):
    monkeypatch.setattr(designer_store, "api_save", lambda body: (400, {"error": "workflow needs an id"}))
    response = admin.route(
        admin_request("PUT", "/api/admin/designer/workflows", {"yaml": "id: [1]\n"}),
        "PUT", "/api/admin/designer/workflows",
    )
    assert response["statusCode"] == 400
    assert "needs an id" in response["body"]


def test_designer_save_without_git_sync_reports_setup(monkeypatch, operator_session, github_env):
    response = admin.route(
        admin_request("PUT", "/api/admin/designer/workflows", {"yaml": WORKFLOW_YAML}),
        "PUT", "/api/admin/designer/workflows",
    )
    assert response["statusCode"] == 503
    assert "not configured" in response["body"]


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


def test_agent_designer_list_and_save_drive_the_same_store(monkeypatch, agent_identity):
    monkeypatch.setattr(designer_store, "api_list", lambda: (200, {"workflows": [], "git_sync": {}}))
    listed = agent_api.route(
        agent_request("GET", "/api/agent/designer/workflows"), "GET", "/api/agent/designer/workflows",
    )
    assert listed["statusCode"] == 200

    calls = []
    monkeypatch.setattr(designer_store, "api_save", lambda body: calls.append(body) or (
        200, {"file": "x.yaml", "commit": "abc", "removed": None},
    ))
    saved = agent_api.route(
        agent_request("PUT", "/api/agent/designer/workflows", {"yaml": WORKFLOW_YAML}),
        "PUT", "/api/agent/designer/workflows",
    )
    assert saved["statusCode"] == 200
    assert calls == [{"yaml": WORKFLOW_YAML}]

    monkeypatch.setattr(
        agent_api, "verify_id_token",
        lambda token, audience=None: (_ for _ in ()).throw(ValueError("bad")),
    )
    assert agent_api.route(
        agent_request("GET", "/api/agent/designer/workflows", token="expired"),
        "GET", "/api/agent/designer/workflows",
    )["statusCode"] == 401


# ---- Static serving ----

def test_designer_page_is_served_with_inline_style_csp():
    response = ingress._static("/designer")
    assert response["statusCode"] == 200
    assert response["headers"]["content-type"].startswith("text/html")
    assert "style-src 'self' 'unsafe-inline'" in response["headers"]["content-security-policy"]
    assert response["headers"]["cache-control"] == "no-store"
    assert 'id="designer-root"' in response["body"]


def test_designer_assets_are_served_cacheable():
    js = ingress._static("/assets/designer.js")
    assert js["statusCode"] == 200
    assert js["headers"]["content-type"].startswith("text/javascript")
    assert js["headers"]["cache-control"] == "public, max-age=300"
    css = ingress._static("/assets/designer.css")
    assert css["statusCode"] == 200
    assert css["headers"]["content-type"].startswith("text/css")


def test_unknown_static_path_is_not_served():
    assert ingress._static("/assets/nope.js") is None
    assert ingress._static("/designer/extra") is None


# ---- CLI parity ----

from dapier_cli import commands as cli_commands


def test_cli_workflows_list_and_show(monkeypatch, capsys):
    def fake_call(api_url, method, path, body=None, **kwargs):
        assert (method, path) == ("GET", "/api/agent/designer/workflows")
        return {
            "workflows": [{
                "id": "test-flow", "enabled": True, "source": "test-flow.yaml",
                "connector": "email", "event": "message.received", "actionCount": 2,
            }],
            "git_sync": {"configured": True, "repo": "DataTalksClub/dapier", "branch": "main"},
        }

    monkeypatch.setattr(cli_commands.api, "call", fake_call)
    assert cli_commands.workflows_list("https://api.example.test") == 0
    out, _ = capsys.readouterr()
    assert "test-flow.yaml" in out
    assert "email.message.received" in out
    assert "DataTalksClub/dapier (main branch)" in out

    def fake_show(api_url, method, path, body=None, **kwargs):
        assert (method, path) == ("GET", "/api/agent/designer/workflows/test-flow.yaml")
        return {"workflow": {"id": "test-flow"}}

    monkeypatch.setattr(cli_commands.api, "call", fake_show)
    assert cli_commands.workflows_show("https://api.example.test", "test-flow.yaml") == 0
    out, _ = capsys.readouterr()
    assert '"id": "test-flow"' in out


def test_cli_workflows_save_posts_yaml_with_rename(monkeypatch, tmp_path, capsys):
    seen = {}

    def fake_call(api_url, method, path, body=None, **kwargs):
        seen.update(method=method, path=path, body=body)
        return {"file": "test-flow.yaml", "commit": "abc1234", "removed": "old.yaml"}

    monkeypatch.setattr(cli_commands.api, "call", fake_call)
    workflow_file = tmp_path / "test-flow.yaml"
    workflow_file.write_text(WORKFLOW_YAML)
    assert cli_commands.workflows_save(
        "https://api.example.test", str(workflow_file), rename_from="old.yaml",
    ) == 0
    assert (seen["method"], seen["path"]) == ("PUT", "/api/agent/designer/workflows")
    assert seen["body"] == {"yaml": WORKFLOW_YAML, "renameFrom": "old.yaml"}
    out, _ = capsys.readouterr()
    assert "Committed test-flow.yaml (abc1234)" in out
    assert cli_commands.workflows_save("https://api.example.test", str(tmp_path / "nope"), None) == 2


# ---- CLI parity ----

from dapier_cli import commands as cli_commands
from src.dapier.auth import session


def test_cli_workflows_list_and_show(monkeypatch, capsys):
    def fake_call(api_url, method, path, body=None, **kwargs):
        assert (method, path) == ("GET", "/api/agent/designer/workflows")
        return {
            "workflows": [{
                "id": "test-flow", "enabled": True, "source": "test-flow.yaml",
                "connector": "email", "event": "message.received", "actionCount": 2,
            }],
            "git_sync": {"configured": True, "repo": "DataTalksClub/dapier", "branch": "main"},
        }

    monkeypatch.setattr(cli_commands.api, "call", fake_call)
    assert cli_commands.workflows_list("https://api.example.test") == 0
    out, _ = capsys.readouterr()
    assert "test-flow.yaml" in out
    assert "email.message.received" in out
    assert "DataTalksClub/dapier (main branch)" in out

    def fake_show(api_url, method, path, body=None, **kwargs):
        assert (method, path) == ("GET", "/api/agent/designer/workflows/test-flow.yaml")
        return {"workflow": {"id": "test-flow"}}

    monkeypatch.setattr(cli_commands.api, "call", fake_show)
    assert cli_commands.workflows_show("https://api.example.test", "test-flow.yaml") == 0
    out, _ = capsys.readouterr()
    assert '"id": "test-flow"' in out


def test_cli_workflows_save_posts_yaml_with_rename(monkeypatch, tmp_path, capsys):
    seen = {}

    def fake_call(api_url, method, path, body=None, **kwargs):
        seen.update(method=method, path=path, body=body)
        return {"file": "test-flow.yaml", "commit": "abc1234", "removed": "old.yaml"}

    monkeypatch.setattr(cli_commands.api, "call", fake_call)
    workflow_file = tmp_path / "test-flow.yaml"
    workflow_file.write_text(WORKFLOW_YAML)
    assert cli_commands.workflows_save(
        "https://api.example.test", str(workflow_file), rename_from="old.yaml",
    ) == 0
    assert (seen["method"], seen["path"]) == ("PUT", "/api/agent/designer/workflows")
    assert seen["body"] == {"yaml": WORKFLOW_YAML, "renameFrom": "old.yaml"}
    out, _ = capsys.readouterr()
    assert "Committed test-flow.yaml (abc1234)" in out
    assert cli_commands.workflows_save("https://api.example.test", str(tmp_path / "nope"), None) == 2


def test_deploy_script_heredoc_terminators_are_literal_lines():
    """bash matches heredoc terminators before expanding parameters, so a
    terminator appended to a ${var} line is never seen: the params file
    swallows the rest of deploy.sh and CI deploys nothing while staying
    green (this shipped exactly that way once)."""
    script = open("scripts/deploy.sh").read()
    starts = len([line for line in script.splitlines() if "<<EOF" in line or "<<'EOF'" in line])
    terminators = len([line for line in script.splitlines() if line.strip() == "EOF"])
    assert starts > 0 and starts == terminators


def test_deploy_script_heredoc_terminators_are_literal_lines():
    """bash matches heredoc terminators before expanding parameters, so a
    terminator appended to a ${var} line is never seen: the params file
    swallows the rest of deploy.sh and CI deploys nothing while staying
    green (this shipped exactly that way once)."""
    script = open("scripts/deploy.sh").read()
    starts = len([line for line in script.splitlines() if "<<EOF" in line or "<<'EOF'" in line])
    terminators = len([line for line in script.splitlines() if line.strip() == "EOF"])
    assert starts > 0 and starts == terminators
