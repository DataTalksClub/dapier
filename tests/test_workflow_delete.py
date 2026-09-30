"""Delete a workflow everywhere it lives: unpublish the live item, remove
its version records, and take the YAML out of the repo in one atomic commit —
the domain path, both route surfaces, and the CLI."""

import json

import pytest

from src.dapier.api import designer_store
from src.dapier.api.designer_store import SyncError
from src.dapier.triggers import published_workflows

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


def seed_live_workflow(table):
    """A live item plus one version record, as a save leaves them."""
    table.put_item({"workflow_id": "test-flow", "workflow": {"id": "test-flow"}})
    table.put_item({"workflow_id": "test-flow#v1", "version_of": "test-flow",
                    "revision": 1})


@pytest.fixture
def github_ready(monkeypatch):
    """Git sync pointed at a scripted GitHub; the token check bypassed."""
    monkeypatch.setenv(designer_store.REPO_URL_ENV, "https://github.com/owner/repo")
    monkeypatch.delenv(designer_store.BRANCH_ENV, raising=False)
    monkeypatch.delenv(designer_store.TOKEN_SECRET_ENV, raising=False)
    monkeypatch.setattr(designer_store, "get_token", lambda: "test-token")
    calls = []

    def fake(method, path, token, payload=None):
        calls.append((method, path, token, payload))
        return GITHUB_SCRIPT[(method, path)]

    monkeypatch.setattr(designer_store, "_github", fake)
    return calls


@pytest.fixture
def bundle(monkeypatch, tmp_path):
    """A deployed bundle carrying exactly the source workflow."""
    monkeypatch.setenv("WORKFLOWS_DIR", str(tmp_path))
    (tmp_path / "test-flow.yaml").write_text(WORKFLOW_YAML)
    return tmp_path


@pytest.fixture
def delayed_gate(monkeypatch):
    """runs.delayed_runs stubbed: [] unless a test parks runs."""
    def fake(workflow_id, limit=25):
        fake.calls.append(workflow_id)
        return fake.parked

    fake.parked = []
    fake.calls = []
    monkeypatch.setattr("src.dapier.api.runs.delayed_runs", fake)
    return fake


def deleted_tree_entries(github_calls):
    """The tree entries of the delete commit, parsed."""
    tree = github_calls[2][3]["tree"]
    return tree


# ---- Domain: designer_store.api_delete ----

def test_delete_unpublishes_tree_deletes_and_keeps_version_history(
        published, github_ready, bundle, delayed_gate):
    seed_live_workflow(published)

    status, payload = designer_store.api_delete("test-flow.yaml", operator="op-1")

    assert status == 200
    assert payload["file"] == "test-flow.yaml"
    assert payload["workflow_id"] == "test-flow"
    assert payload["deleted"] is True
    assert payload["published"] is False
    assert payload["was_published"] is True
    assert payload["commit"] == "commit456"

    # The live item is gone; the #v<n> version records stay — history, not
    # live state, the same way past runs stay readable.
    assert "test-flow" not in published.items
    assert "test-flow#v1" in published.items
    # One atomic commit removes exactly workflows/test-flow.yaml.
    assert [entry["path"] for entry in deleted_tree_entries(github_ready)] == [
        "workflows/test-flow.yaml"]
    assert github_ready[-1] == ("PATCH", "/repos/owner/repo/git/refs/heads/main",
                                "test-token", {"sha": "commit456"})
    # The delay gate ran against the workflow's id.
    assert delayed_gate.calls == ["test-flow"]


def test_delete_refuses_while_runs_are_parked(
        published, github_ready, bundle, delayed_gate):
    seed_live_workflow(published)
    delayed_gate.parked = [{"run_id": "abc", "delayed_until": "2026-10-01T00:00:00Z"}]

    status, payload = designer_store.api_delete("test-flow.yaml")

    assert status == 409
    assert "parked run(s) on a delay" in payload["error"]
    assert payload["delayed_runs"] == delayed_gate.parked
    assert "test-flow" in published.items  # still live
    assert github_ready == []  # nothing was committed


def test_delete_of_an_unpublished_workflow_is_not_found(
        published, github_ready, bundle, delayed_gate):
    status, payload = designer_store.api_delete("test-flow.yaml")

    assert status == 404
    assert "no such workflow" in payload["error"]
    assert github_ready == []


def test_delete_is_a_404_once_the_file_is_gone_from_git(
        published, github_ready, bundle, delayed_gate, monkeypatch):
    """A workflow that is neither live nor in git is already gone: a second
    delete (deploy lag or not) is a 404, never a stray commit."""
    monkeypatch.setenv(designer_store.TOKEN_SECRET_ENV, "test-secret")

    def no_such_file(filename, token=None):
        raise KeyError(filename)

    monkeypatch.setattr(designer_store, "fetch_workflow", no_such_file)
    status, payload = designer_store.api_delete("test-flow.yaml")

    assert status == 404
    assert "no such workflow" in payload["error"]
    assert github_ready == []


def test_delete_reports_git_failure_but_stays_deleted(
        published, github_ready, bundle, delayed_gate, monkeypatch):
    seed_live_workflow(published)

    def failing(method, path, token, payload=None):
        if method == "POST" and path.endswith("/git/trees"):
            raise SyncError("HTTP 422: pull request is not mergeable")
        return GITHUB_SCRIPT[(method, path)]

    monkeypatch.setattr(designer_store, "_github", failing)
    status, payload = designer_store.api_delete("test-flow.yaml")

    assert status == 200
    assert payload["deleted"] is True
    assert "HTTP 422" in payload["git_sync_error"]
    assert "commit" not in payload
    assert "test-flow" not in published.items  # the engine stopped matching it anyway
    assert "test-flow#v1" in published.items  # history is untouched


def test_delete_missing_workflow_is_404(published, github_ready, bundle, delayed_gate):
    status, payload = designer_store.api_delete("nope.yaml")
    assert status == 404
    # api_get's last-resort git probe may run, but nothing was ever committed.
    assert [call for call in github_ready if call[0] in ("POST", "PATCH")] == []


@pytest.mark.parametrize("source", ["../escape.yaml", "no-extension", ""])
def test_delete_rejects_bad_file_names(published, github_ready, bundle, delayed_gate, source):
    status, payload = designer_store.api_delete(source)
    assert status == 400
    assert "invalid workflow file name" in payload["error"]
    assert github_ready == []
    assert published.items == {}


def test_delete_unpublish_failure_is_502(published, github_ready, bundle, delayed_gate,
                                         monkeypatch):
    seed_live_workflow(published)

    def broken(workflow_id, table_ref=None):
        raise RuntimeError("table is throttling")

    monkeypatch.setattr(published_workflows, "unpublish", broken)
    status, payload = designer_store.api_delete("test-flow.yaml")

    assert status == 502
    assert "throttling" in payload["error"]
    assert github_ready == []  # never left the workflow half-deleted in git
    assert "test-flow" in published.items  # untouched


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


def test_admin_delete_route_drives_the_same_store(monkeypatch, operator_session):
    calls = []
    monkeypatch.setattr(designer_store, "api_delete", lambda source, operator=None:
                        calls.append({"source": source, "operator": operator})
                        or (200, {"file": source, "deleted": True}))
    audits = []
    monkeypatch.setattr(session, "_audit_event",
                        lambda *args, **kwargs: audits.append((args, kwargs)) or None)

    response = admin.route(
        admin_request("DELETE", "/api/admin/designer/workflows/test-flow.yaml"),
        "DELETE", "/api/admin/designer/workflows/test-flow.yaml",
    )
    assert response["statusCode"] == 200
    assert calls == [{"source": "test-flow.yaml", "operator": "op-1"}]
    assert audits[0][0][1] == "workflow.delete"


def test_agent_delete_route_drives_the_same_store(monkeypatch, agent_identity):
    calls = []
    monkeypatch.setattr(designer_store, "api_delete", lambda source, operator=None:
                        calls.append({"source": source, "operator": operator})
                        or (200, {"file": source, "deleted": True}))

    response = agent_api.route(
        agent_request("DELETE", "/api/agent/designer/workflows/test-flow.yaml"),
        "DELETE", "/api/agent/designer/workflows/test-flow.yaml",
    )
    assert response["statusCode"] == 200
    # The agent route audits the operator itself; the store call needs no name.
    assert calls == [{"source": "test-flow.yaml", "operator": None}]

    monkeypatch.setattr(
        agent_api, "verify_id_token",
        lambda token, audience=None: (_ for _ in ()).throw(ValueError("bad")),
    )
    assert agent_api.route(
        agent_request("DELETE", "/api/agent/designer/workflows/test-flow.yaml",
                      token="expired"),
        "DELETE", "/api/agent/designer/workflows/test-flow.yaml",
    )["statusCode"] == 401


def test_agent_delete_end_to_end(published, github_ready, bundle, delayed_gate,
                                 agent_identity):
    seed_live_workflow(published)

    response = agent_api.route(
        agent_request("DELETE", "/api/agent/designer/workflows/test-flow.yaml"),
        "DELETE", "/api/agent/designer/workflows/test-flow.yaml",
    )
    assert response["statusCode"] == 200
    payload = json.loads(response["body"])
    assert payload["deleted"] is True
    assert payload["was_published"] is True
    assert payload["commit"] == "commit456"
    assert "test-flow" not in published.items
    assert "test-flow#v1" in published.items


# ---- CLI ----

from dapier_cli import commands as cli_commands


def test_cli_delete_calls_the_agent_endpoint(monkeypatch, capsys):
    seen = {}

    def fake_call(api_url, method, path, body=None, **kwargs):
        seen.update(method=method, path=path)
        return {"file": "test-flow.yaml", "deleted": True, "was_published": True,
                "commit": "commit456"}

    monkeypatch.setattr(cli_commands.api, "call", fake_call)
    assert cli_commands.workflows_delete(
        "https://api.example.test", "test-flow.yaml", assume_yes=True) == 0
    assert (seen["method"], seen["path"]) == (
        "DELETE", "/api/agent/designer/workflows/test-flow.yaml")
    out, _ = capsys.readouterr()
    assert "Deleted test-flow.yaml" in out
    assert "Committed the removal (commit4)" in out


def test_cli_delete_aborts_without_confirmation(monkeypatch, capsys):
    monkeypatch.setattr("builtins.input", lambda prompt: "n")
    called = []
    monkeypatch.setattr(cli_commands.api, "call",
                        lambda *args, **kwargs: called.append(args) or {})

    assert cli_commands.workflows_delete(
        "https://api.example.test", "test-flow.yaml") == 1
    assert called == []
    out, _ = capsys.readouterr()
    assert "Cancelled" in out


# ---- Complementary coverage: the engine/listing effects, the real
# delayed-run gate, operator gating, and CLI error surfacing. ----

import base64  # noqa: E402

import boto3  # noqa: E402
from botocore.exceptions import ClientError  # noqa: E402

from dapier_cli.api import ApiError  # noqa: E402
from src.dapier.api import overview  # noqa: E402
from src.dapier.api import runs  # noqa: E402
from src.dapier.engine.matching import all_workflows  # noqa: E402


class StatefulGit:
    """The GitHub git-data API as state: tree writes add files, ``sha: None``
    entries remove them (the tree-delete a delete commit sends), and a
    contents GET answers from that state — 404 once gone. The second delete's
    git-truth check reads exactly this."""

    def __init__(self):
        self.files = {}
        self.calls = []

    def __call__(self, method, path, token, payload=None):
        self.calls.append((method, path, payload))
        if method == "GET" and path.endswith("/git/ref/heads/main"):
            return {"object": {"sha": "base123"}}
        if method == "GET" and "/git/commits/" in path:
            return {"tree": {"sha": "treesh"}}
        if method == "POST" and path.endswith("/git/trees"):
            for entry in payload["tree"]:
                # A delete entry carries an explicit ``sha: None``; a write
                # entry has no sha key at all.
                if "sha" in entry and entry["sha"] is None:
                    self.files.pop(entry["path"], None)
                else:
                    self.files[entry["path"]] = entry["content"]
            return {"sha": "newtree"}
        if method == "POST" and path.endswith("/git/commits"):
            return {"sha": "commit456",
                    "html_url": "https://github.com/owner/repo/commit/commit456"}
        if method == "PATCH" and path.endswith("/git/refs/heads/main"):
            return {}
        if method == "GET" and "/contents/" in path:
            name = path.split("/contents/")[1].split("?")[0]
            if name in self.files:
                return {"encoding": "base64",
                        "content": base64.b64encode(self.files[name].encode()).decode()}
            raise SyncError(f"github refused GET {path}: HTTP 404 not found")
        raise AssertionError(f"unexpected github call: {method} {path}")


@pytest.fixture
def git_state(monkeypatch):
    """Git sync configured against the stateful fake (token env on, like
    production, so delete's git-existence check is active)."""
    monkeypatch.setenv(designer_store.REPO_URL_ENV, "https://github.com/owner/repo")
    monkeypatch.delenv(designer_store.BRANCH_ENV, raising=False)
    monkeypatch.setenv(designer_store.TOKEN_SECRET_ENV, "dapier/designer/token")
    monkeypatch.setattr(designer_store, "get_token", lambda: "test-token")
    git = StatefulGit()
    monkeypatch.setattr(designer_store, "_github", git)
    return git


@pytest.fixture
def empty_bundle(monkeypatch, tmp_path):
    """A deploy-time bundle carrying nothing: a designer-created workflow
    lives only in the published store, so 'no longer matches' is observable."""
    monkeypatch.setenv("WORKFLOWS_DIR", str(tmp_path))
    return tmp_path


def _save():
    assert designer_store.api_save(
        {"yaml": WORKFLOW_YAML}, operator="op-1", live=True)[0] == 200


def _tree_posts(git):
    return [call for call in git.calls
            if call[0] == "POST" and call[1].endswith("/git/trees")]


@pytest.fixture
def no_parked_runs(monkeypatch):
    """The executions ledger wired and empty: the real delayed_runs gate
    walks it and finds nothing parked (FakeExecutionsTable is below)."""
    monkeypatch.setenv("EXECUTIONS_TABLE", "executions")
    table = FakeExecutionsTable([])

    class Dynamo:
        def Table(self, _name):
            return table

    monkeypatch.setattr(boto3, "resource", lambda service: Dynamo())
    return table


def test_delete_stops_event_matching_and_disappears_from_listings(
        git_state, published, empty_bundle, no_parked_runs):
    _save()
    assert any(workflow.get("id") == "test-flow" for workflow in all_workflows())
    assert [wf["id"] for wf in overview._workflows()] == ["test-flow"]
    assert [row["id"] for row in designer_store.api_list()[1]["workflows"]] == ["test-flow"]

    assert designer_store.api_delete("test-flow.yaml", operator="op-2")[0] == 200

    assert all(workflow.get("id") != "test-flow" for workflow in all_workflows())
    assert overview._workflows() == []
    assert designer_store.api_list()[1]["workflows"] == []
    # The live item is gone; the version records stay as history.
    assert "test-flow" not in published.items
    assert "test-flow#v1" in published.items


def test_a_second_delete_is_a_404_even_with_deploy_lag(git_state, published,
                                                       empty_bundle, no_parked_runs):
    _save()
    assert designer_store.api_delete("test-flow.yaml")[0] == 200
    commits_so_far = [call for call in git_state.calls
                      if call[0] == "POST" and call[1].endswith("/git/trees")]
    assert len(commits_so_far) == 2  # the save's write, then the delete
    # Deploy lag: the repo working tree (and any deployed bundle built from
    # it) still carries the file even though git and live do not.
    (empty_bundle / "test-flow.yaml").write_text(WORKFLOW_YAML)

    status, payload = designer_store.api_delete("test-flow.yaml")

    assert status == 404
    assert "no such workflow" in payload["error"]
    # The retry attempted no stray third commit.
    assert [call for call in git_state.calls
            if call[0] == "POST" and call[1].endswith("/git/trees")] == commits_so_far


def _equality_pairs(condition):
    """(attribute, value) pairs from the Attr().eq() chains the runs module
    sends — a single condition or an ``&`` of them."""
    pairs = []
    for part in getattr(condition, "_values", ()) or ():
        operands = getattr(part, "_values", ()) or ()
        attr = next((o for o in operands if hasattr(o, "name")), None)
        value = next((o for o in operands if not hasattr(o, "name")), None)
        if attr is not None:
            pairs.append((attr.name, value))
    return pairs


class FakeExecutionsTable:
    """The executions ledger, just wide enough for the gate: scan applies
    the workflow+delayed filter delayed_runs sends, query answers the
    runs-by-run-id GSI for api_get, and update_item honors api_cancel's
    delayed-only conditional write."""

    def __init__(self, items):
        self.items = list(items)

    def scan(self, **kwargs):
        wanted = dict(_equality_pairs(kwargs.get("FilterExpression")))

        def matches(item):
            return all(item.get(name) == value for name, value in wanted.items())

        return {"Items": [item for item in self.items if matches(item)]}

    def query(self, **kwargs):
        wanted = dict(_equality_pairs(kwargs.get("KeyConditionExpression")))
        run_id = wanted.get("run_id")
        return {"Items": [item for item in self.items if item.get("run_id") == run_id]}

    def update_item(self, Key, UpdateExpression=None, ConditionExpression=None,
                    ExpressionAttributeNames=None, ExpressionAttributeValues=None, **_):
        item = next((i for i in self.items
                     if i.get("execution_id") == Key["execution_id"]), None)
        if item is None:
            raise ClientError({"Error": {"Code": "ResourceNotFoundException",
                                         "Message": "unknown execution"}}, "UpdateItem")
        if ConditionExpression:
            # The one conditional write the module sends: "#status = :delayed".
            names = ExpressionAttributeNames or {}
            values = ExpressionAttributeValues or {}
            target, expected = (part.strip()
                                for part in ConditionExpression.split(" = "))
            name = names.get(target, target)
            if item.get(name) != values.get(expected, expected):
                raise ClientError(
                    {"Error": {"Code": "ConditionalCheckFailedException",
                               "Message": "the step is no longer parked"}},
                    "UpdateItem")
        if UpdateExpression and "SET" in UpdateExpression:
            names = ExpressionAttributeNames or {}
            values = ExpressionAttributeValues or {}
            for assignment in UpdateExpression.split("SET", 1)[1].split(","):
                target, placeholder = assignment.strip().split(" = ")
                item[names.get(target, target)] = values[placeholder]
        return {}


@pytest.fixture
def parked_run(monkeypatch):
    """One delayed step of run test-flow:evt-1 in the executions ledger; the
    real runs.delayed_runs and runs.api_cancel run against it."""
    monkeypatch.setenv("EXECUTIONS_TABLE", "executions")
    table = FakeExecutionsTable([{
        "execution_id": "test-flow:pause:evt-1",
        "run_id": "test-flow:evt-1",
        "workflow_id": "test-flow",
        "action_id": "pause",
        "status": "delayed",
        "started_at": "2026-09-28T09:00:00+00:00",
        "output": {"resume_at": "2026-09-28T10:00:00+00:00"},
    }])

    class Dynamo:
        def Table(self, _name):
            return table

    monkeypatch.setattr(boto3, "resource", lambda service: Dynamo())
    return table


def test_delete_refuses_while_a_run_is_parked_and_succeeds_after_cancel(
        git_state, published, empty_bundle, parked_run):
    _save()
    after_save = _tree_posts(git_state)

    status, payload = designer_store.api_delete("test-flow.yaml")

    assert status == 409
    assert "parked" in payload["error"]
    assert payload["delayed_runs"] == [{
        "run_id": "test-flow:evt-1",
        "delayed_until": "2026-09-28T10:00:00+00:00",
    }]
    # Nothing was deleted: still live, still in git, no commit attempted.
    assert "test-flow" in published.items
    assert "workflows/test-flow.yaml" in git_state.files
    assert _tree_posts(git_state) == after_save

    # The operator cancels the suspended run (the real runs.api_cancel path
    # flips the parked step to cancelled), and the delete goes through.
    status, payload = runs.api_cancel("test-flow:evt-1")
    assert status == 200
    assert payload["cancelled"] == 1

    status, payload = designer_store.api_delete("test-flow.yaml")
    assert status == 200
    assert "test-flow" not in published.items  # live item gone, versions kept
    assert "workflows/test-flow.yaml" not in git_state.files


def test_agent_delete_is_operator_gated(monkeypatch, agent_identity, git_state,
                                        published, empty_bundle):
    monkeypatch.setenv("OPERATOR_SUBJECTS", "someone-else")
    emitted = []
    monkeypatch.setattr(agent_api, "audit", type("Audit", (), {
        "emit": staticmethod(
            lambda *args, **kwargs: emitted.append((args, kwargs)) or None),
    }))
    _save()

    response = agent_api.route(
        agent_request("DELETE", "/api/agent/designer/workflows/test-flow.yaml"),
        "DELETE", "/api/agent/designer/workflows/test-flow.yaml",
    )

    assert response["statusCode"] == 403
    # Nothing was touched.
    assert "test-flow" in published.items
    assert "workflows/test-flow.yaml" in git_state.files
    assert emitted[0][0][1] == "workflow.delete"
    assert emitted[0][1]["outcome"] == "denied-not-operator"


def test_cli_delete_surfaces_the_parked_run_409(monkeypatch):
    def refused(api_url, method, path, body=None, **kwargs):
        raise ApiError("workflow test-flow still has 1 parked run(s) on a delay "
                       "— cancel or wait them out before deleting", status=409)

    monkeypatch.setattr(cli_commands.api, "call", refused)
    with pytest.raises(ApiError) as excinfo:
        cli_commands.workflows_delete(
            "https://api.example.test", "test-flow.yaml", assume_yes=True)
    assert excinfo.value.status == 409
    assert "parked run" in str(excinfo.value)


def test_cli_delete_without_a_terminal_refuses_instead_of_crashing(monkeypatch, capsys):
    calls = []

    def fake_call(api_url, method, path, body=None, **kwargs):
        calls.append((method, path))
        return {"file": "test-flow.yaml", "deleted": True}

    monkeypatch.setattr(cli_commands.api, "call", fake_call)

    def closed_stdin(prompt=""):
        raise EOFError  # no terminal: scripts, CI, a closed pipe

    monkeypatch.setattr("builtins.input", closed_stdin)
    assert cli_commands.workflows_delete("https://api.example.test", "test-flow.yaml") == 2
    out, _ = capsys.readouterr()
    assert "--yes" in out
    assert calls == []


def test_version_history_survives_delete_and_the_endpoint_still_answers(
        git_state, published, empty_bundle, no_parked_runs, operator_session):
    """The #v<n> records are history, not live state: a deleted id keeps its
    version list, nothing flagged current, and the versions route answers
    200 for it instead of a 404-or-500."""
    _save()
    revised = WORKFLOW_YAML.replace("https://example.test/hook", "https://example.test/hook2")
    assert designer_store.api_save({"yaml": revised}, operator="op-2", live=True)[0] == 200
    assert designer_store.api_delete("test-flow.yaml", operator="op-3")[0] == 200

    status, payload = designer_store.api_versions("test-flow.yaml")
    assert status == 200
    assert payload["workflow"] == "test-flow"
    assert payload["revision"] == 0  # nothing live any more
    assert [version["revision"] for version in payload["versions"]] == [2, 1]
    assert [version["cause"] for version in payload["versions"]] == ["save", "save"]
    assert all(not version["current"] for version in payload["versions"])

    # The route surface answers the same — never a 500 for a deleted id.
    response = admin.route(
        admin_request("GET", "/api/admin/designer/workflows/test-flow.yaml/versions"),
        "GET", "/api/admin/designer/workflows/test-flow.yaml/versions",
    )
    assert response["statusCode"] == 200
    body = json.loads(response["body"])
    assert [version["revision"] for version in body["versions"]] == [2, 1]
    assert body["versions"][0]["published_by"] == "op-2"


def test_delete_accepts_a_disabled_workflow(git_state, published, empty_bundle,
                                            no_parked_runs):
    """Zapier deletes live zaps and paused zaps alike: the enabled flag is
    not a gate — the parked-run refusal is the only one."""
    _save()
    assert designer_store.api_toggle(
        "test-flow.yaml", {"enabled": False}, operator="op-1")[0] == 200

    status, payload = designer_store.api_delete("test-flow.yaml", operator="op-2")

    assert status == 200
    assert payload["deleted"] is True
    assert payload["was_published"] is True
    assert "test-flow" not in published.items
    assert "workflows/test-flow.yaml" not in git_state.files
