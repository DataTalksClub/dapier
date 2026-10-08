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


def test_workflows_require_the_managed_store(monkeypatch):
    """A checkout no longer supplies live workflow definitions."""
    from src.dapier.triggers import published_workflows

    monkeypatch.delenv(published_workflows.TABLE_ENV, raising=False)
    status, payload = designer_store.api_list()
    assert status == 200
    assert payload["workflows"] == []
    status, payload = designer_store.api_get("example.yaml")
    assert status == 404


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
    ("id: x\ntrigger: {connector: email, event: e}\nactions: [{type: code}]\n", "non-empty code"),
    ("id: x\ntrigger: {connector: email, event: e}\nactions: [{type: code, code: ''}]\n", "non-empty code"),
    ("id: x\ntrigger: {connector: email, event: e}\nactions: [{type: code, code: '   '}]\n", "non-empty code"),
    ("id: x\ntrigger: {connector: [nope, event: e}\nactions: []\n", "invalid YAML"),
])
def test_parse_workflow_rejects_invalid_definitions(yaml_text, fragment):
    with pytest.raises(designer_store.WorkflowError, match=fragment):
        designer_store.parse_workflow(yaml_text)


def test_parse_workflow_rejects_oversized_yaml():
    with pytest.raises(designer_store.WorkflowError, match="too large"):
        designer_store.parse_workflow("id: x\n# " + "x" * (designer_store.MAX_YAML_BYTES + 10))


def test_parse_workflow_accepts_a_code_action_with_source():
    workflow = designer_store.parse_workflow(
        "id: coder\n"
        "trigger: {connector: email, event: message.received}\n"
        "actions:\n"
        "  - id: shape\n"
        "    type: code\n"
        "    code: |\n"
        "      {'route': input['route']}\n")
    action = workflow["actions"][0]
    assert action["type"] == "code"
    assert "route" in action["code"]


def test_parse_workflow_accepts_triggers_list_with_inline_actions():
    workflow = designer_store.parse_workflow(
        "id: multi\n"
        "triggers:\n"
        "  - {connector: email, event: message.received}\n"
        "  - {connector: dropbox, event: file.created}\n"
        "actions: [{type: webhook, url: 'https://intake.test/x'}]\n")
    assert [trigger["connector"] for trigger in workflow["triggers"]] == ["email", "dropbox"]
    assert workflow["actions"][0]["type"] == "webhook"


@pytest.mark.parametrize("yaml_text,fragment", [
    ("id: x\ntrigger: {connector: email, event: e}\nflow: shared\nactions: [{type: webhook, url: 'https://x'}]\n",
     "shared flows are retired"),
    ("id: x\ntrigger: {connector: email, event: e}\nflow: nope\n",
     "shared flows are retired"),
    ("id: x\ntriggers: [{connector: email}]\nactions: [{type: webhook, url: 'https://x'}]\n",
     "connector and an event"),
])
def test_parse_workflow_rejects_bad_multi_trigger_and_flow_shapes(yaml_text, fragment):
    with pytest.raises(designer_store.WorkflowError, match=fragment):
        designer_store.parse_workflow(yaml_text)


LOGIC_YAML = """\
id: logic-flow
enabled: true
trigger:
  connector: email
  event: message.received
actions:
  - id: gate
    type: filter
    field: route
    operator: equals
    value: invoice
  - id: route
    type: condition
    when:
      subject: {contains: urgent}
    then:
      - {id: notify, type: slack, channel: '#alerts', text: '{subject}'}
    else:
      - {id: pause, type: delay, seconds: 30}
  - id: each
    type: for_each
    list: attachments
    item: item
    actions:
      - id: upload
        type: dropbox_upload
        connection_id: dropbox
        folder: "/Invoices/{item.filename}"
"""


def test_parse_workflow_accepts_logic_steps():
    workflow = designer_store.parse_workflow(LOGIC_YAML)
    kinds = [action["type"] for action in workflow["actions"]]
    assert kinds == ["filter", "condition", "for_each"]
    assert workflow["actions"][2]["actions"][0]["folder"] == "/Invoices/{item.filename}"


def test_parse_workflow_accepts_paths_step():
    workflow = designer_store.parse_workflow(
        "id: x\ntrigger: {connector: email, event: e}\n"
        "actions:\n"
        "  - id: route\n"
        "    type: paths\n"
        "    paths:\n"
        "      - label: invoices\n"
        "        when: {subject: {contains: invoice}}\n"
        "        actions: [{id: a, type: webhook, url: 'https://x'}]\n"
        "    default: [{id: b, type: webhook, url: 'https://y'}]\n")
    step = workflow["actions"][0]
    assert step["paths"][0]["label"] == "invoices"
    assert step["default"][0]["id"] == "b"


@pytest.mark.parametrize("yaml_text,fragment", [
    ("id: x\ntrigger: {connector: email, event: e}\n"
     "actions: [{id: pause, type: delay, seconds: 0}]\n",
     "needs seconds"),
    ("id: x\ntrigger: {connector: email, event: e}\n"
     "actions: [{id: pause, type: delay, seconds: -5}]\n",
     "non-negative number"),
    ("id: x\ntrigger: {connector: email, event: e}\n"
     "actions: [{id: pause, type: delay, seconds: 'later'}]\n",
     "must be a number or a"),
    ("id: x\ntrigger: {connector: email, event: e}\n"
     "actions: [{id: pause, type: delay}]\n",
     "needs seconds"),
    ("id: x\ntrigger: {connector: email, event: e}\n"
     "actions: [{id: pause, type: delay, until: 'not-a-date'}]\n",
     "ISO 8601"),
    ("id: x\ntrigger: {connector: email, event: e}\n"
     "actions: [{id: pause, type: delay, until: '2026-10-01T09:00:00Z',\n"
     "  seconds: 30}]\n",
     "not both"),
    ("id: x\ntrigger: {connector: email, event: e}\n"
     "actions: [{id: pause, type: delay, days: 100}]\n",
     "may not exceed 90 days"),
    ("id: x\ntrigger: {connector: email, event: e}\n"
     "actions: [{id: gate, type: filter, operator: equals, value: invoice}]\n",
     "needs a when mapping or a field"),
    ("id: x\ntrigger: {connector: email, event: e}\n"
     "actions: [{id: gate, type: filter, field: route, operator: regex, value: '.'}]\n",
     "operator must be one of"),
    ("id: x\ntrigger: {connector: email, event: e}\n"
     "actions: [{id: gate, type: filter, when: [route]}]\n",
     "must be a mapping"),
    ("id: x\ntrigger: {connector: email, event: e}\n"
     "actions: [{id: route, type: condition, field: route, then: {id: a}}]\n",
     "must be a list of steps"),
    ("id: x\ntrigger: {connector: email, event: e}\n"
     "actions: [{id: route, type: condition, field: route,\n"
     "  then: [{id: pause, type: delay, seconds: 'later'}]}]\n",
     "must be a number or a"),
    ("id: x\ntrigger: {connector: email, event: e}\n"
     "actions: [{id: each, type: for_each, actions: [{id: a, type: webhook, url: 'https://x'}]}]\n",
     "needs a list field"),
    ("id: x\ntrigger: {connector: email, event: e}\n"
     "actions: [{id: each, type: for_each, list: attachments}]\n",
     "at least one step"),
    ("id: x\ntrigger: {connector: email, event: e}\n"
     "actions: [{id: each, type: for_each, list: attachments, item: '9bad',\n"
     "  actions: [{id: a, type: webhook, url: 'https://x'}]}]\n",
     "template variable name"),
    ("id: x\ntrigger: {connector: email, event: e}\n"
     "actions: [{id: each, type: for_each, list: attachments, max_iterations: 101,\n"
     "  actions: [{id: a, type: webhook, url: 'https://x'}]}]\n",
     "between 1 and 100"),
    ("id: x\ntrigger: {connector: email, event: e}\n"
     "actions: [{id: each, type: for_each, list: attachments,\n"
     "  actions: [{type: ''}]}]\n",
     "needs a type"),
    ("id: x\ntrigger: {connector: email, event: e}\n"
     "actions: [{id: route, type: paths, paths: []}]\n",
     "non-empty list of paths"),
    ("id: x\ntrigger: {connector: email, event: e}\n"
     "actions: [{id: route, type: paths, paths: [{label: a}]}]\n",
     "needs a when mapping or a field"),
    ("id: x\ntrigger: {connector: email, event: e}\n"
     "actions: [{id: route, type: paths, paths: [{label: a, when: {s: {equals: 1}}}],\n"
     "  default: {id: b}}]\n",
     "default must be a list of steps"),
])
def test_parse_workflow_rejects_invalid_logic_steps(yaml_text, fragment):
    with pytest.raises(designer_store.WorkflowError, match=fragment):
        designer_store.parse_workflow(yaml_text)


@pytest.mark.parametrize("delay_step", [
    "seconds: 30",                       # sleeps inline
    "seconds: 3600",                     # suspends past the inline cap — legal now
    "minutes: 5",
    "hours: 2 + minutes: 12",
    "days: 1, hours: 12",
    "until: 2026-10-01T09:00:00Z",
    "until: '{next_run}'",               # rendered when the step runs
    "seconds: '{wait}'",
])
def test_parse_workflow_accepts_long_delay_shapes(delay_step):
    fields = ", ".join(delay_step.replace(" + ", ", ").split(", "))
    workflow = designer_store.parse_workflow(
        "id: x\ntrigger: {connector: email, event: e}\n"
        f"actions: [{{id: pause, type: delay, {fields}}}]\n")
    assert workflow["actions"][0]["type"] == "delay"


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


def _managed(monkeypatch, *yaml_texts):
    workflows = [designer_store.parse_workflow(text) for text in yaml_texts]
    items = [{"workflow_id": workflow["id"], "workflow": workflow,
              "file": f"{workflow['id']}.yaml", "revision": 1}
             for workflow in workflows]
    monkeypatch.setenv(published_workflows.TABLE_ENV, "published-test")
    monkeypatch.setattr(published_workflows, "load_items",
                        lambda table_ref=None, include_drafts=False: items)
    monkeypatch.setattr(published_workflows, "get_item", lambda workflow_id, table_ref=None:
                        next((item for item in items if item["workflow_id"] == workflow_id), None))
    monkeypatch.setattr(published_workflows, "get_draft",
                        lambda workflow_id, table_ref=None: None)


def test_designer_list_reports_managed_workflows_and_sync(monkeypatch, operator_session):
    _managed(monkeypatch, WORKFLOW_YAML)
    monkeypatch.delenv(designer_store.TOKEN_SECRET_ENV, raising=False)
    monkeypatch.setenv(designer_store.REPO_URL_ENV, "https://github.com/owner/repo")

    response = admin.route(
        admin_request("GET", "/api/admin/designer/workflows"), "GET", "/api/admin/designer/workflows",
    )
    payload = json.loads(response["body"])
    assert response["statusCode"] == 200
    assert [item["source"] for item in payload["workflows"]] == ["test-flow.yaml"]
    assert payload["workflows"][0]["actionCount"] == 2
    assert payload["git_sync"] == {"configured": False, "repo": "owner/repo", "branch": "main"}


def test_designer_get_serves_managed_yaml_and_404(monkeypatch, operator_session):
    _managed(monkeypatch, WORKFLOW_YAML)
    monkeypatch.delenv(designer_store.TOKEN_SECRET_ENV, raising=False)

    found = admin.route(
        admin_request("GET", "/api/admin/designer/workflows/test-flow.yaml"),
        "GET", "/api/admin/designer/workflows/test-flow.yaml",
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
    monkeypatch.setattr(designer_store, "api_save", lambda body, operator=None: (
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
    monkeypatch.setattr(designer_store, "api_save", lambda body, operator=None: (400, {"error": "workflow needs an id"}))
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
    monkeypatch.setattr(designer_store, "api_list",
                        lambda q=None, tag=None, folder=None, visible=None:
                        (200, {"workflows": [], "git_sync": {}}))
    listed = agent_api.route(
        agent_request("GET", "/api/agent/designer/workflows"), "GET", "/api/agent/designer/workflows",
    )
    assert listed["statusCode"] == 200

    calls = []
    monkeypatch.setattr(designer_store, "api_save", lambda body, operator=None: calls.append(body) or (
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
    response = ingress._static("/designer/app")
    assert response["statusCode"] == 200
    assert response["headers"]["content-type"].startswith("text/html")
    assert "style-src 'self' 'unsafe-inline'" in response["headers"]["content-security-policy"]
    assert response["headers"]["cache-control"] == "no-store"
    assert 'id="designer-root"' in response["body"]


def test_designer_assets_are_served_uncached():
    # Deploys swap the bundles in place, so a cached copy would drive stale
    # markup/JS against fresh pages; everything is no-store.
    js = ingress._static("/assets/designer.js")
    assert js["statusCode"] == 200
    assert js["headers"]["content-type"].startswith("text/javascript")
    assert js["headers"]["cache-control"] == "no-store"
    css = ingress._static("/assets/designer.css")
    assert css["statusCode"] == 200
    assert css["headers"]["content-type"].startswith("text/css")
    assert css["headers"]["cache-control"] == "no-store"


def test_unknown_static_path_is_not_served():
    assert ingress._static("/assets/nope.js") is None
    assert ingress._static("/designer/extra") is None


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
    assert "test-flow" in out and "NAME" in out and "ID" in out
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
    assert "Saved test-flow.yaml" in out
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




def test_designer_toggle_routes_to_the_store(monkeypatch, operator_session):
    toggled = {}
    monkeypatch.setattr(designer_store, "api_toggle", lambda source, body, operator=None: (
        toggled.update(source=source, body=body, operator=operator)
        or (200, {"file": source, "enabled": body["enabled"]})
    ))
    audits = []
    monkeypatch.setattr(session, "_audit_event",
                        lambda *args, **kwargs: audits.append((args, kwargs)) or None)

    response = admin.route(
        admin_request("PUT", "/api/admin/designer/workflows/test-flow.yaml", {"enabled": False}),
        "PUT", "/api/admin/designer/workflows/test-flow.yaml",
    )
    assert response["statusCode"] == 200
    assert toggled == {"source": "test-flow.yaml", "body": {"enabled": False}, "operator": "op-1"}
    assert audits[0][0][1] == "workflow.toggle"


def test_agent_designer_toggle_drives_the_same_store(monkeypatch, agent_identity):
    toggled = {}
    monkeypatch.setattr(designer_store, "api_toggle", lambda source, body, operator=None: (
        toggled.update(source=source, body=body, operator=operator)
        or (200, {"file": source, "enabled": body["enabled"]})
    ))
    response = agent_api.route(
        agent_request("PUT", "/api/agent/designer/workflows/test-flow.yaml", {"enabled": False}),
        "PUT", "/api/agent/designer/workflows/test-flow.yaml",
    )
    assert response["statusCode"] == 200
    assert toggled["source"] == "test-flow.yaml"
    assert toggled["body"] == {"enabled": False}
    assert toggled["operator"] == "agent-op"


# ---- Workflow version history and rollback ----

def test_designer_versions_routes_to_the_store(monkeypatch, operator_session):
    seen = {}
    monkeypatch.setattr(designer_store, "api_versions", lambda source, visible=None: (
        seen.update(source=source) or (200, {"versions": []})))
    response = admin.route(
        admin_request("GET", "/api/admin/designer/workflows/test-flow.yaml/versions"),
        "GET", "/api/admin/designer/workflows/test-flow.yaml/versions",
    )
    assert response["statusCode"] == 200
    assert seen == {"source": "test-flow.yaml"}


def test_designer_rollback_routes_to_the_store(monkeypatch, operator_session):
    seen = {}
    audits = []
    monkeypatch.setattr(designer_store, "api_rollback", lambda source, body, operator=None: (
        seen.update(source=source, body=body, operator=operator)
        or (200, {"file": source, "published": True})
    ))
    monkeypatch.setattr(session, "_audit_event",
                        lambda *args, **kwargs: audits.append((args, kwargs)) or None)
    response = admin.route(
        admin_request("POST", "/api/admin/designer/workflows/test-flow.yaml/rollback",
                      {"revision": 2}),
        "POST", "/api/admin/designer/workflows/test-flow.yaml/rollback",
    )
    assert response["statusCode"] == 200
    assert seen == {"source": "test-flow.yaml", "body": {"revision": 2}, "operator": "op-1"}
    assert audits[0][0][1] == "workflow.rollback"


def test_agent_designer_versions_and_rollback_drive_the_same_store(monkeypatch, agent_identity):
    seen = {}
    monkeypatch.setattr(designer_store, "api_versions", lambda source, visible=None: (
        seen.update(versions_source=source) or (200, {"versions": []})))
    monkeypatch.setattr(designer_store, "api_rollback", lambda source, body, operator=None: (
        seen.update(rollback_source=source, rollback_body=body, rollback_operator=operator)
        or (200, {"file": source, "published": True})
    ))
    listed = agent_api.route(
        agent_request("GET", "/api/agent/designer/workflows/test-flow.yaml/versions"),
        "GET", "/api/agent/designer/workflows/test-flow.yaml/versions",
    )
    rolled = agent_api.route(
        agent_request("POST", "/api/agent/designer/workflows/test-flow.yaml/rollback",
                      {"revision": 1}),
        "POST", "/api/agent/designer/workflows/test-flow.yaml/rollback",
    )
    assert listed["statusCode"] == 200
    assert rolled["statusCode"] == 200
    assert seen["versions_source"] == "test-flow.yaml"
    assert seen["rollback_source"] == "test-flow.yaml"
    assert seen["rollback_body"] == {"revision": 1}
    assert seen["rollback_operator"] == "agent-op"


def test_cli_workflows_versions_and_rollback(monkeypatch, capsys):
    calls = []

    def fake_call(api_url, method, path, body=None, **kwargs):
        calls.append((method, path, body))
        if method == "GET":
            return {
                "workflow": "test-flow",
                "revision": 3,
                "versions": [
                    {"revision": 3, "published_at": "2026-09-28T10:00:00+00:00",
                     "cause": "rollback", "published_by": "op-3", "enabled": True,
                     "current": True},
                    {"revision": 2, "published_at": "2026-09-27T09:00:00+00:00",
                     "cause": "toggle", "published_by": "op-2", "enabled": False,
                     "current": False},
                ],
            }
        return {"file": "test-flow.yaml", "commit": "abc1234", "published": True}

    monkeypatch.setattr(cli_commands.api, "call", fake_call)
    assert cli_commands.workflows_versions("https://api.example.test", "test-flow.yaml") == 0
    out, _ = capsys.readouterr()
    assert "revision 3" in out
    assert "v3" in out and "v2" in out
    assert "(current)" in out and "rollback" in out

    assert cli_commands.workflows_rollback("https://api.example.test", "test-flow.yaml", 2) == 0
    out, _ = capsys.readouterr()
    assert calls[-1] == ("POST", "/api/agent/designer/workflows/test-flow.yaml/rollback",
                         {"revision": 2})
    assert "Rolled test-flow.yaml back to v2" in out
    assert "published it live" in out

    # No revision named: the API restores the version before the live one.
    assert cli_commands.workflows_rollback("https://api.example.test", "test-flow.yaml", None) == 0
    out, _ = capsys.readouterr()
    assert calls[-1] == ("POST", "/api/agent/designer/workflows/test-flow.yaml/rollback", {})
    assert "previous version" in out


# ---- Version history and rollback: store behavior ----

from src.dapier.triggers import published_workflows


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
    """The publish table configured and stubbed; version records included."""
    monkeypatch.setenv(published_workflows.TABLE_ENV, "published-test")
    table = HistoryStubTable()
    monkeypatch.setattr(published_workflows, "get_table", lambda table_ref=None: table)
    return table


@pytest.fixture
def git_sync(monkeypatch):
    """Git sync scripted end to end; every commit's message and YAML captured."""
    monkeypatch.setenv(designer_store.REPO_URL_ENV, "https://github.com/owner/repo")
    monkeypatch.setenv(designer_store.TOKEN_SECRET_ENV, "test-secret")
    monkeypatch.delenv(designer_store.BRANCH_ENV, raising=False)
    monkeypatch.setattr(designer_store, "get_token", lambda: "test-token")
    commits = []

    def github(method, path, token, payload=None):
        if method == "POST" and path.endswith("/git/trees"):
            commits.append({"blob": payload["tree"][0]["content"]})
        if method == "POST" and path.endswith("/git/commits"):
            commits[-1]["message"] = payload["message"]
        return GITHUB_SCRIPT[(method, path)]

    monkeypatch.setattr(designer_store, "_github", github)
    return commits


def test_two_saves_stamp_revisions_and_keep_history(git_sync, history_store):
    assert designer_store.api_save({"yaml": WORKFLOW_YAML}, operator="op-1", live=True)[0] == 200
    revised = WORKFLOW_YAML.replace("https://example.test/hook", "https://example.test/hook2")
    assert designer_store.api_save({"yaml": revised}, operator="op-2", live=True)[0] == 200

    live = published_workflows.get_item("test-flow")
    assert live["revision"] == 2
    versions = published_workflows.list_versions("test-flow")
    assert [v["revision"] for v in versions] == [2, 1]
    assert all(v["cause"] == "save" for v in versions)
    assert versions[0]["published_by"] == "op-2"
    assert versions[0]["workflow"]["actions"][0]["url"] == "https://example.test/hook2"
    assert versions[1]["workflow"]["actions"][0]["url"] == "https://example.test/hook"


def test_versions_endpoint_lists_newest_first_and_flags_live(git_sync, history_store, operator_session):
    designer_store.api_save({"yaml": WORKFLOW_YAML}, operator="op-1", live=True)
    revised = WORKFLOW_YAML.replace("https://example.test/hook", "https://example.test/hook2")
    designer_store.api_save({"yaml": revised}, operator="op-2", live=True)

    response = admin.route(
        admin_request("GET", "/api/admin/designer/workflows/test-flow.yaml/versions"),
        "GET", "/api/admin/designer/workflows/test-flow.yaml/versions",
    )
    payload = json.loads(response["body"])
    assert response["statusCode"] == 200
    assert payload["workflow"] == "test-flow"
    assert payload["revision"] == 2
    assert [v["revision"] for v in payload["versions"]] == [2, 1]
    assert [v["current"] for v in payload["versions"]] == [True, False]
    assert payload["versions"][1]["published_by"] == "op-1"


def test_rollback_round_trips_the_previous_yaml(git_sync, history_store):
    assert designer_store.api_save({"yaml": WORKFLOW_YAML}, operator="op-1", live=True)[0] == 200
    revised = WORKFLOW_YAML.replace("https://example.test/hook", "https://example.test/hook2")
    assert designer_store.api_save({"yaml": revised}, operator="op-2", live=True)[0] == 200

    status, payload = designer_store.api_rollback("test-flow.yaml", {}, operator="op-3")
    assert status == 200
    assert payload["published"] is True
    assert payload["commit"] == "commit456"

    live = published_workflows.get_item("test-flow")
    assert live["revision"] == 3
    assert live["workflow"]["actions"][0]["url"] == "https://example.test/hook"
    assert live["published_by"] == "op-3"
    # The restore went through the save path: one new commit whose YAML is
    # the restored definition, recorded in the history as a rollback.
    assert git_sync[-1]["message"] == "designer: rollback workflow test-flow to v1"
    assert "url: https://example.test/hook\n" in git_sync[-1]["blob"]
    versions = published_workflows.list_versions("test-flow")
    assert [v["revision"] for v in versions] == [3, 2, 1]
    assert versions[0]["cause"] == "rollback"


def test_rollback_to_an_explicit_revision_restores_its_enabled_flag(git_sync, history_store):
    assert designer_store.api_save({"yaml": WORKFLOW_YAML}, operator="op-1", live=True)[0] == 200
    assert designer_store.api_toggle("test-flow.yaml", {"enabled": False}, operator="op-2")[0] == 200

    status, _ = designer_store.api_rollback("test-flow.yaml", {"revision": 2}, operator="op-3")
    assert status == 200
    assert published_workflows.get_item("test-flow")["enabled"] is False

    status, _ = designer_store.api_rollback("test-flow.yaml", {"revision": 1}, operator="op-3")
    assert status == 200
    live = published_workflows.get_item("test-flow")
    assert live["enabled"] is True
    assert live["revision"] == 4
    assert [v["revision"] for v in published_workflows.list_versions("test-flow")] == [4, 3, 2, 1]


def test_rollback_error_paths(git_sync, history_store):
    designer_store.api_save({"yaml": WORKFLOW_YAML}, operator="op-1", live=True)
    assert designer_store.api_rollback("test-flow.yaml", {})[0] == 409  # nothing before v1
    assert designer_store.api_rollback("test-flow.yaml", {"revision": 9})[0] == 404
    assert designer_store.api_rollback("test-flow.yaml", {"revision": "nope"})[0] == 400
    assert designer_store.api_rollback("test-flow.yaml", {"revision": 1.5})[0] == 400
    assert designer_store.api_rollback("../etc", {"revision": 1})[0] == 400


def test_rollback_without_the_publish_table_is_503(git_sync, monkeypatch):
    monkeypatch.delenv(published_workflows.TABLE_ENV, raising=False)
    assert designer_store.api_rollback("test-flow.yaml", {"revision": 1})[0] == 503


def test_version_history_prunes_to_the_newest_versions(git_sync, history_store, monkeypatch):
    monkeypatch.setattr(published_workflows, "MAX_VERSIONS", 3)
    for index in range(5):
        assert designer_store.api_save(
            {"yaml": WORKFLOW_YAML.replace("hook", f"hook{index}")}, operator="op", live=True)[0] == 200

    assert [v["revision"] for v in published_workflows.list_versions("test-flow")] == [5, 4, 3]
    assert "test-flow#v2" not in history_store.items
    assert "test-flow#v1" not in history_store.items
    # The live item and its revision stamp survive pruning.
    assert published_workflows.get_item("test-flow")["revision"] == 5


def test_agent_versions_and_rollback_round_trip_over_bearer(git_sync, history_store, agent_identity):
    designer_store.api_save({"yaml": WORKFLOW_YAML}, operator="op-1", live=True)
    revised = WORKFLOW_YAML.replace("https://example.test/hook", "https://example.test/hook2")
    designer_store.api_save({"yaml": revised}, operator="op-2", live=True)

    listed = agent_api.route(
        agent_request("GET", "/api/agent/designer/workflows/test-flow.yaml/versions"),
        "GET", "/api/agent/designer/workflows/test-flow.yaml/versions",
    )
    assert listed["statusCode"] == 200
    payload = json.loads(listed["body"])
    assert payload["workflow"] == "test-flow"
    assert [v["revision"] for v in payload["versions"]] == [2, 1]

    rolled = agent_api.route(
        agent_request("POST", "/api/agent/designer/workflows/test-flow.yaml/rollback",
                      {"revision": 1}),
        "POST", "/api/agent/designer/workflows/test-flow.yaml/rollback",
    )
    assert rolled["statusCode"] == 200
    assert json.loads(rolled["body"])["published"] is True
    live = published_workflows.get_item("test-flow")
    assert live["revision"] == 3
    assert live["workflow"]["actions"][0]["url"] == "https://example.test/hook"


# ---- Export/import symmetry and server-side search ----

def test_api_get_returns_canonical_reusable_yaml(monkeypatch, tmp_path):
    _managed(monkeypatch, WORKFLOW_YAML)

    status, payload = designer_store.api_get("test-flow.yaml")
    assert status == 200
    yaml_text = payload["yaml"]
    assert "id: test-flow" in yaml_text
    # The export re-saves byte-equivalent: dumping the parsed export is the
    # same text, which is what a save would commit.
    assert designer_store.workflow_yaml_text(
        designer_store.parse_workflow(yaml_text)) == yaml_text


def test_summary_carries_description_and_action_types(monkeypatch, tmp_path):
    _managed(monkeypatch,
        "id: described\ndescription: Ship the weekly digest\n"
        "trigger: {connector: email, event: message.received}\n"
        "actions: [{type: slack}, {type: webhook, url: 'https://x'}]\n")

    _, payload = designer_store.api_list()
    row = payload["workflows"][0]
    assert row["description"] == "Ship the weekly digest"
    assert row["actionTypes"] == ["slack", "webhook"]


def test_api_list_search_filters_on_id_description_trigger_and_types(monkeypatch, tmp_path):
    _managed(monkeypatch,
        "id: invoice-alert\ndescription: Alert on new invoices\n"
        "trigger: {connector: email, event: message.received}\n"
        "actions: [{type: slack, channel: '#ops'}]\n",
        "id: nightly-backup\ndescription: Copy files to S3\n"
        "trigger: {connector: schedule, event: tick}\n"
        "actions: [{type: dropbox_upload}]\n")

    _, all_rows = designer_store.api_list()
    assert [row["id"] for row in all_rows["workflows"]] == ["invoice-alert", "nightly-backup"]

    _, by_id = designer_store.api_list("INVOICE")   # id, case-insensitive
    assert [row["id"] for row in by_id["workflows"]] == ["invoice-alert"]
    _, by_description = designer_store.api_list("copy files")
    assert [row["id"] for row in by_description["workflows"]] == ["nightly-backup"]
    _, by_trigger = designer_store.api_list("dropbox")
    assert [row["id"] for row in by_trigger["workflows"]] == ["nightly-backup"]
    _, by_type = designer_store.api_list("slack")
    assert [row["id"] for row in by_type["workflows"]] == ["invoice-alert"]
    _, no_match = designer_store.api_list("nothing-matches-this")
    assert no_match["workflows"] == []


def test_designer_list_passes_the_search_to_the_store(monkeypatch, operator_session):
    seen = {}
    monkeypatch.setattr(designer_store, "api_list",
                        lambda q=None, tag=None, folder=None, visible=None:
                        seen.update(q=q, tag=tag)
                        or (200, {"workflows": [], "git_sync": {}}))
    event = admin_request("GET", "/api/admin/designer/workflows")
    event["queryStringParameters"] = {"q": "invoice"}
    admin.route(event, "GET", "/api/admin/designer/workflows")
    assert seen["q"] == "invoice"
    assert seen["tag"] is None

    event["queryStringParameters"] = {"tag": "ops"}
    admin.route(event, "GET", "/api/admin/designer/workflows")
    assert seen["tag"] == "ops"


def test_agent_designer_list_passes_the_search_to_the_store(monkeypatch, agent_identity):
    seen = {}
    monkeypatch.setattr(designer_store, "api_list",
                        lambda q=None, tag=None, folder=None, visible=None:
                        seen.update(q=q, tag=tag)
                        or (200, {"workflows": [], "git_sync": {}}))
    event = agent_request("GET", "/api/agent/designer/workflows")
    event["queryStringParameters"] = {"q": "backup"}
    agent_api.route(event, "GET", "/api/agent/designer/workflows")
    assert seen["q"] == "backup"
    assert seen["tag"] is None

    event["queryStringParameters"] = {"tag": "ops"}
    agent_api.route(event, "GET", "/api/agent/designer/workflows")
    assert seen["tag"] == "ops"


def test_legacy_gallery_flag_is_removed_on_import():
    workflow = designer_store.parse_workflow(WORKFLOW_YAML.replace(
        "id: test-flow", "id: test-flow\ntemplate: true"))
    assert "template" not in workflow
    assert "template" not in designer_store._summary(workflow, "test-flow.yaml")


@pytest.mark.parametrize("method,suffix", [
    ("GET", "/templates"),
    ("POST", "/templates/starter.yaml/apply"),
    ("PUT", "/workflows/starter.yaml/template"),
])
def test_removed_template_routes(method, suffix, operator_session, agent_identity):
    for module, request, prefix in [
        (admin, admin_request, "/api/admin/designer"),
        (agent_api, agent_request, "/api/agent/designer"),
    ]:
        path = prefix + suffix
        assert module.route(request(method, path), method, path)["statusCode"] == 404
