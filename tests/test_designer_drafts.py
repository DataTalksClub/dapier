"""G15 Draft vs live: a save drafts, publish/discard promote or throw.

A designer save writes a ``<id>#draft`` item (draft_of, workflow,
base_revision; one per workflow, last write wins) and touches nothing live:
no publish, no git commit, no YouTube reconcile, no stored-trigger
registration. ``load_items`` drops drafts, so the engine and every list are
draft-blind — a draft-only workflow fires nothing. The publish route promotes
the draft through the ordinary save path (cause "publish": git, revision and
YouTube reconcile for free), refusing 409 stale when the live definition has
moved past the draft's base revision.
"""

import json

import pytest

from src.dapier.api import admin
from src.dapier.api import agent as agent_api
from src.dapier.api import designer_store
from src.dapier.engine import matching
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

REVISED_YAML = WORKFLOW_YAML.replace("https://example.test/hook",
                                     "https://example.test/hook2")

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


class DraftStubTable:
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
def store(monkeypatch):
    """The publish table configured and stubbed; the real one never touched."""
    monkeypatch.setenv(published_workflows.TABLE_ENV, "published-test")
    table = DraftStubTable()
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
            # A delete commit's tree entry carries sha: None, not content.
            commits.append({"blob": (payload["tree"][0] or {}).get("content")})
        if method == "POST" and path.endswith("/git/commits"):
            commits[-1]["message"] = payload["message"]
        return GITHUB_SCRIPT[(method, path)]

    monkeypatch.setattr(designer_store, "_github", github)
    return commits


# ---- Save writes a draft and touches nothing live ---------------------------

def test_save_writes_a_draft_item_and_nothing_live(git_sync, store):
    status, payload = designer_store.api_save({"yaml": WORKFLOW_YAML}, operator="op-1")
    assert status == 200
    assert payload["published"] is False
    assert payload["file"] == "test-flow.yaml"
    assert payload["draft"]["base_revision"] == 0

    draft = published_workflows.get_draft("test-flow")
    assert draft["draft_of"] == "test-flow"
    assert draft["workflow"]["id"] == "test-flow"
    assert draft["base_revision"] == 0
    assert draft["drafted_by"] == "op-1"
    # Nothing live: no published item, no version records, no git commits.
    assert published_workflows.get_item("test-flow") is None
    assert published_workflows.list_versions("test-flow") == []
    assert git_sync == []


def test_save_is_last_write_wins_and_refreshes_the_base_revision(git_sync, store):
    assert designer_store.api_save(
        {"yaml": WORKFLOW_YAML}, operator="op-1", live=True)[0] == 200
    assert designer_store.api_save({"yaml": REVISED_YAML}, operator="op-2")[0] == 200
    assert designer_store.api_save({"yaml": WORKFLOW_YAML}, operator="op-3")[0] == 200

    drafts = [item for item in store.items.values() if item.get("draft_of")]
    assert len(drafts) == 1
    draft = published_workflows.get_draft("test-flow")
    assert draft["workflow"]["actions"][0]["url"] == "https://example.test/hook"
    assert draft["base_revision"] == 1  # the live revision it was edited against
    assert draft["drafted_by"] == "op-3"
    # The live item never moved.
    assert published_workflows.get_item("test-flow")["revision"] == 1


def test_draft_save_does_not_reconcile_youtube(store, monkeypatch):
    calls = []
    monkeypatch.setattr(designer_store, "_sync_youtube",
                        lambda *, previous=None, workflow=None: calls.append(workflow) or [])
    assert designer_store.api_save({"yaml": WORKFLOW_YAML}, operator="op")[0] == 200
    assert calls == []  # a draft is not a live definition change


def test_engine_and_loaders_are_draft_blind(git_sync, store):
    assert designer_store.api_save({"yaml": WORKFLOW_YAML}, operator="op-1")[0] == 200

    # The single choke point: load_items drops drafts, so the engine's
    # matching chain (load_workflows -> load_items) never sees them.
    assert [item["workflow_id"] for item in published_workflows.load_items()] == []
    assert matching.workflows() == []
    assert published_workflows.load_workflows() == []
    # ...and the draft-only workflow lists as published: false.
    status, payload = designer_store.api_list()
    assert status == 200
    assert [row["id"] for row in payload["workflows"]] == ["test-flow"]
    assert payload["workflows"][0]["published"] is False
    assert "has_draft" not in payload["workflows"][0]

    # Stored trigger tables are a separate store; a draft save never writes
    # there (publish() is the only writer to the live item, and it did not run).
    assert not any(item.get("version_of") for item in store.items.values())


def test_live_and_draft_rows_merge_in_the_list(git_sync, store):
    assert designer_store.api_save({"yaml": WORKFLOW_YAML}, operator="op-1", live=True)[0] == 200
    assert designer_store.api_save({"yaml": REVISED_YAML}, operator="op-2")[0] == 200
    assert designer_store.api_save(
        {"yaml": WORKFLOW_YAML.replace("test-flow", "draft-only")},
        operator="op-3")[0] == 200

    status, payload = designer_store.api_list()
    rows = {row["id"]: row for row in payload["workflows"]}
    assert rows["test-flow"]["published"] is True
    assert rows["test-flow"]["has_draft"] is True
    assert rows["draft-only"]["published"] is False
    assert "has_draft" not in rows["draft-only"]


def test_draft_read_serves_the_drafted_definition(store):
    assert designer_store.api_save({"yaml": WORKFLOW_YAML}, operator="op-1")[0] == 200
    status, payload = designer_store.api_draft("test-flow.yaml")
    assert status == 200
    assert payload["published"] is False
    assert payload["workflow"]["id"] == "test-flow"
    assert payload["draft"]["base_revision"] == 0
    assert "id: test-flow" in payload["yaml"]
    # The live read still answers 404 — nothing is published.
    assert designer_store.api_get("test-flow.yaml")[0] == 404


# ---- Publish promotes the draft through the ordinary save path --------------

def test_publish_promotes_a_draft_only_workflow_as_v1(git_sync, store):
    assert designer_store.api_save({"yaml": WORKFLOW_YAML}, operator="op-1")[0] == 200

    status, payload = designer_store.api_publish("test-flow.yaml", operator="op-2")
    assert status == 200
    assert payload["published"] is True
    assert payload["revision"] == 1
    assert payload["commit"] == "commit456"

    live = published_workflows.get_item("test-flow")
    assert live["workflow"]["actions"][0]["url"] == "https://example.test/hook"
    assert live["published_by"] == "op-2"
    versions = published_workflows.list_versions("test-flow")
    assert [v["revision"] for v in versions] == [1]
    assert versions[0]["cause"] == "publish"
    assert git_sync[-1]["message"] == "designer: save workflow test-flow"
    # The draft is consumed.
    assert published_workflows.get_draft("test-flow") is None


def test_publish_promotes_a_revision_bump_and_clears_the_draft_block(git_sync, store):
    assert designer_store.api_save({"yaml": WORKFLOW_YAML}, operator="op-1", live=True)[0] == 200
    assert designer_store.api_save({"yaml": REVISED_YAML}, operator="op-2")[0] == 200
    _, versions = designer_store.api_versions("test-flow.yaml")
    assert versions["draft"]["stale"] is False

    status, payload = designer_store.api_publish("test-flow.yaml", operator="op-2")
    assert status == 200
    assert payload["revision"] == 2
    live = published_workflows.get_item("test-flow")
    assert live["workflow"]["actions"][0]["url"] == "https://example.test/hook2"
    _, versions = designer_store.api_versions("test-flow.yaml")
    assert "draft" not in versions


def test_publish_carries_a_draft_rename_through_the_save_path(git_sync, store):
    assert designer_store.api_save({"yaml": WORKFLOW_YAML}, operator="op-1", live=True)[0] == 200
    renamed = WORKFLOW_YAML.replace("test-flow", "renamed-flow")
    assert designer_store.api_save(
        {"yaml": renamed, "renameFrom": "test-flow.yaml"}, operator="op-2")[0] == 200
    # The draft save did not touch the live original.
    assert published_workflows.get_item("test-flow")["revision"] == 1

    status, _ = designer_store.api_publish("renamed-flow.yaml", operator="op-2")
    assert status == 200
    assert published_workflows.get_item("renamed-flow") is not None
    assert published_workflows.get_item("test-flow") is None  # renamed away live


def test_publish_refuses_a_stale_draft(git_sync, store):
    assert designer_store.api_save({"yaml": WORKFLOW_YAML}, operator="op-1", live=True)[0] == 200
    assert designer_store.api_save({"yaml": REVISED_YAML}, operator="op-2")[0] == 200
    # A toggle raced the draft: live moved from v1 to v2.
    assert designer_store.api_toggle("test-flow.yaml", {"enabled": False}, operator="op-3")[0] == 200

    status, payload = designer_store.api_publish("test-flow.yaml", operator="op-2")
    assert status == 409
    assert payload["reason"] == "stale"
    assert payload["base_revision"] == 1
    assert payload["revision"] == 2
    # The refusal keeps both sides: the draft and the newer live definition.
    assert published_workflows.get_draft("test-flow")["base_revision"] == 1
    assert published_workflows.get_item("test-flow")["revision"] == 2


def test_a_fresh_save_makes_a_stale_draft_publishable_again(git_sync, store):
    assert designer_store.api_save({"yaml": WORKFLOW_YAML}, operator="op-1", live=True)[0] == 200
    assert designer_store.api_save({"yaml": REVISED_YAML}, operator="op-2")[0] == 200
    assert designer_store.api_toggle("test-flow.yaml", {"enabled": False}, operator="op-3")[0] == 200
    assert designer_store.api_publish("test-flow.yaml")[0] == 409

    # Saving again re-bases the draft on the current live revision (LWW).
    assert designer_store.api_save({"yaml": REVISED_YAML}, operator="op-2")[0] == 200
    status, payload = designer_store.api_publish("test-flow.yaml", operator="op-2")
    assert status == 200
    assert payload["revision"] == 3


def test_publish_error_paths(store):
    assert designer_store.api_save({"yaml": WORKFLOW_YAML}, operator="op")[0] == 200
    assert designer_store.api_publish("nope.yaml")[0] == 404      # no such draft
    assert designer_store.api_publish("../etc")[0] == 400
    designer_store.api_discard("test-flow.yaml")
    assert designer_store.api_publish("test-flow.yaml")[0] == 404  # draft consumed


# ---- Discard, diff, versions draft block, delete ----------------------------

def test_discard_throws_the_draft_and_keeps_live(git_sync, store):
    assert designer_store.api_save({"yaml": WORKFLOW_YAML}, operator="op-1", live=True)[0] == 200
    assert designer_store.api_save({"yaml": REVISED_YAML}, operator="op-2")[0] == 200

    status, payload = designer_store.api_discard("test-flow.yaml", operator="op-3")
    assert status == 200
    assert payload["discarded"] is True
    assert published_workflows.get_draft("test-flow") is None
    live = published_workflows.get_item("test-flow")
    assert live["revision"] == 1
    assert live["workflow"]["actions"][0]["url"] == "https://example.test/hook"
    assert designer_store.api_discard("test-flow.yaml")[0] == 404


def test_draft_diff_reuses_the_api_diff_shape(git_sync, store):
    assert designer_store.api_save({"yaml": WORKFLOW_YAML}, operator="op-1", live=True)[0] == 200

    # Identical draft: same is true and the diff is empty.
    assert designer_store.api_save({"yaml": WORKFLOW_YAML}, operator="op-2")[0] == 200
    status, payload = designer_store.api_draft_diff("test-flow.yaml")
    assert status == 200
    assert payload["workflow"] == "test-flow"
    assert payload["from"]["revision"] == 1
    assert payload["to"]["revision"] == "draft"
    assert payload["same"] is True
    assert payload["diff"] == ""
    assert payload["truncated"] is False

    # A changed draft diffs against live.
    assert designer_store.api_save({"yaml": REVISED_YAML}, operator="op-2")[0] == 200
    _, payload = designer_store.api_draft_diff("test-flow.yaml")
    assert payload["same"] is False
    assert "hook2" in payload["diff"]


def test_draft_diff_for_a_draft_only_workflow_diffs_against_nothing(store):
    assert designer_store.api_save({"yaml": WORKFLOW_YAML}, operator="op-1")[0] == 200
    status, payload = designer_store.api_draft_diff("test-flow.yaml")
    assert status == 200
    assert payload["from"]["revision"] == 0
    assert payload["from"]["yaml"] == ""
    assert payload["same"] is False
    assert "id: test-flow" in payload["diff"]


def test_draft_diff_without_a_draft_is_404(store):
    assert designer_store.api_draft_diff("nope.yaml")[0] == 404


def test_versions_list_gains_a_draft_block(git_sync, store):
    assert designer_store.api_save({"yaml": WORKFLOW_YAML}, operator="op-1", live=True)[0] == 200
    _, versions = designer_store.api_versions("test-flow.yaml")
    assert "draft" not in versions  # nothing drafted

    assert designer_store.api_save({"yaml": REVISED_YAML}, operator="op-2")[0] == 200
    _, versions = designer_store.api_versions("test-flow.yaml")
    assert versions["draft"]["base_revision"] == 1
    assert versions["draft"]["stale"] is False
    assert versions["draft"]["drafted_by"] == "op-2"

    # A live toggle stales the draft block.
    assert designer_store.api_toggle("test-flow.yaml", {"enabled": False}, operator="op-3")[0] == 200
    _, versions = designer_store.api_versions("test-flow.yaml")
    assert versions["draft"]["stale"] is True


def test_delete_removes_the_draft_with_the_workflow(git_sync, store, monkeypatch):
    from src.dapier.api import runs

    monkeypatch.setattr(runs, "delayed_runs", lambda workflow_id: [])
    assert designer_store.api_save({"yaml": WORKFLOW_YAML}, operator="op-1", live=True)[0] == 200
    assert designer_store.api_save({"yaml": REVISED_YAML}, operator="op-2")[0] == 200

    status, _ = designer_store.api_delete("test-flow.yaml", operator="op-3")
    assert status == 200
    assert published_workflows.get_item("test-flow") is None
    assert published_workflows.get_draft("test-flow") is None


def test_toggle_tags_folder_and_rollback_stale_the_draft_but_keep_it(git_sync, store):
    assert designer_store.api_save({"yaml": WORKFLOW_YAML}, operator="op-1", live=True)[0] == 200
    assert designer_store.api_save({"yaml": REVISED_YAML}, operator="op-2")[0] == 200

    assert designer_store.api_tags("test-flow.yaml", {"tags": ["ops"]}, operator="op-3")[0] == 200
    assert published_workflows.get_draft("test-flow")["base_revision"] == 1
    assert published_workflows.get_item("test-flow")["revision"] == 2

    assert designer_store.api_folder("test-flow.yaml", {"folder": "Money"}, operator="op-3")[0] == 200
    assert published_workflows.get_item("test-flow")["revision"] == 3
    assert published_workflows.get_draft("test-flow") is not None

    assert designer_store.api_rollback("test-flow.yaml", {"revision": 1}, operator="op-3")[0] == 200
    assert published_workflows.get_item("test-flow")["revision"] == 4
    # The draft survives every live verb — publish refuses it until a fresh
    # save re-bases it.
    assert designer_store.api_publish("test-flow.yaml")[0] == 409


def test_auto_pause_stales_a_draft(git_sync, store, monkeypatch):
    assert designer_store.api_save({"yaml": WORKFLOW_YAML}, operator="op-1", live=True)[0] == 200
    assert designer_store.api_save({"yaml": REVISED_YAML}, operator="op-2")[0] == 200

    paused = designer_store.api_auto_pause("test-flow", error="boom")
    assert paused is not None
    assert published_workflows.get_item("test-flow")["revision"] == 2
    assert published_workflows.get_draft("test-flow")["base_revision"] == 1
    assert designer_store.api_publish("test-flow.yaml")[0] == 409


def test_a_draft_only_workflow_fires_nothing_even_when_enabled(git_sync, store, monkeypatch):
    monkeypatch.setattr(published_workflows, "SCAN_LIMIT", 2)  # exercise paging too
    assert designer_store.api_save({"yaml": WORKFLOW_YAML}, operator="op-1")[0] == 200
    assert matching.all_workflows() == []


# ---- Routes: admin + agent parity -------------------------------------------

def admin_request(method, path, body=None):
    return {
        "requestContext": {"http": {"method": method, "path": path}},
        "headers": {"host": "dapier.example.test"},
        "cookies": [],
        "body": json.dumps(body) if body is not None else None,
    }


@pytest.fixture
def operator_session(monkeypatch):
    from src.dapier.auth import session

    monkeypatch.setattr(session, "authenticated", lambda event: True)
    monkeypatch.setattr(session, "_csrf_ok", lambda event, method: True)
    monkeypatch.setattr(session, "require_operator", lambda event: ({"sub": "op-1"}, None))
    monkeypatch.setattr(session, "require_role",
                        lambda event, minimum="operator": ({"sub": "op-1"}, None))
    audits = []
    monkeypatch.setattr(session, "_audit_event",
                        lambda *args, **kwargs: audits.append((args, kwargs)) or None)
    return audits


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


def test_admin_draft_routes_drive_the_store(git_sync, store, operator_session):
    assert designer_store.api_save({"yaml": WORKFLOW_YAML}, operator="op-1")[0] == 200

    read = admin.route(
        admin_request("GET", "/api/admin/designer/workflows/test-flow.yaml/draft"),
        "GET", "/api/admin/designer/workflows/test-flow.yaml/draft",
    )
    assert read["statusCode"] == 200
    assert json.loads(read["body"])["workflow"]["id"] == "test-flow"

    diff = admin.route(
        admin_request("GET", "/api/admin/designer/workflows/test-flow.yaml/draft/diff"),
        "GET", "/api/admin/designer/workflows/test-flow.yaml/draft/diff",
    )
    assert diff["statusCode"] == 200
    assert json.loads(diff["body"])["to"]["revision"] == "draft"

    published = admin.route(
        admin_request("POST", "/api/admin/designer/workflows/test-flow.yaml/publish"),
        "POST", "/api/admin/designer/workflows/test-flow.yaml/publish",
    )
    assert published["statusCode"] == 200
    assert json.loads(published["body"])["revision"] == 1
    audits = operator_session
    assert any(args[0][1] == "workflow.publish" for args in audits)

    # Discard after publish: the draft is gone, so 404.
    discarded = admin.route(
        admin_request("DELETE", "/api/admin/designer/workflows/test-flow.yaml/draft"),
        "DELETE", "/api/admin/designer/workflows/test-flow.yaml/draft",
    )
    assert discarded["statusCode"] == 404
    assert any(args[0][1] == "workflow.discard" for args in audits)


def test_agent_draft_routes_drive_the_same_store(git_sync, store, agent_identity,
                                                 monkeypatch):
    assert designer_store.api_save({"yaml": WORKFLOW_YAML}, operator="op-1")[0] == 200

    diff = agent_api.route(
        agent_request("GET", "/api/agent/designer/workflows/test-flow.yaml/draft/diff"),
        "GET", "/api/agent/designer/workflows/test-flow.yaml/draft/diff",
    )
    assert diff["statusCode"] == 200

    discarded = agent_api.route(
        agent_request("DELETE", "/api/agent/designer/workflows/test-flow.yaml/draft"),
        "DELETE", "/api/agent/designer/workflows/test-flow.yaml/draft",
    )
    assert discarded["statusCode"] == 200
    assert json.loads(discarded["body"])["discarded"] is True

    published = agent_api.route(
        agent_request("POST", "/api/agent/designer/workflows/test-flow.yaml/publish"),
        "POST", "/api/agent/designer/workflows/test-flow.yaml/publish",
    )
    assert published["statusCode"] == 404  # the draft was just discarded

    # Operator-gated like its siblings: a bad bearer never reaches the store.
    monkeypatch.setattr(
        agent_api, "verify_id_token",
        lambda token, audience=None: (_ for _ in ()).throw(ValueError("bad")),
    )
    denied = agent_api.route(
        agent_request("POST", "/api/agent/designer/workflows/test-flow.yaml/publish",
                      token="expired"),
        "POST", "/api/agent/designer/workflows/test-flow.yaml/publish",
    )
    assert denied["statusCode"] == 401


def test_agent_draft_routes_promote_end_to_end(git_sync, store, agent_identity):
    assert designer_store.api_save({"yaml": WORKFLOW_YAML}, operator="op-1")[0] == 200
    published = agent_api.route(
        agent_request("POST", "/api/agent/designer/workflows/test-flow.yaml/publish"),
        "POST", "/api/agent/designer/workflows/test-flow.yaml/publish",
    )
    assert published["statusCode"] == 200
    assert published_workflows.get_item("test-flow")["revision"] == 1
    assert published_workflows.get_draft("test-flow") is None


def test_stale_publish_409_travels_through_both_surfaces(git_sync, store, operator_session,
                                                         agent_identity):
    assert designer_store.api_save({"yaml": WORKFLOW_YAML}, operator="op-1", live=True)[0] == 200
    assert designer_store.api_save({"yaml": REVISED_YAML}, operator="op-2")[0] == 200
    assert designer_store.api_toggle("test-flow.yaml", {"enabled": False}, operator="op-3")[0] == 200

    for route in (admin, agent_api):
        response = route.route(
            admin_request("POST", "/api/admin/designer/workflows/test-flow.yaml/publish")
            if route is admin else
            agent_request("POST", "/api/agent/designer/workflows/test-flow.yaml/publish"),
            "POST", "/api/admin/designer/workflows/test-flow.yaml/publish"
            if route is admin else
            "/api/agent/designer/workflows/test-flow.yaml/publish",
        )
        assert response["statusCode"] == 409
        assert json.loads(response["body"])["reason"] == "stale"


# ---- CLI parity ---------------------------------------------------------------

from dapier_cli import commands as cli_commands


def test_cli_publish_discard_and_draft_diff_drive_the_agent_api(monkeypatch, capsys):
    calls = []

    def fake_call(api_url, method, path, body=None, **kwargs):
        calls.append((method, path, body))
        if method == "GET":
            return {"file": "test-flow.yaml", "workflow": "test-flow",
                    "from": {"revision": 1, "yaml": "id: test-flow\n"},
                    "to": {"revision": "draft", "yaml": "id: test-flow\n"},
                    "diff": "", "same": True, "truncated": False}
        return {"file": "test-flow.yaml", "published": True, "revision": 2,
                "commit": "abc1234"}

    monkeypatch.setattr(cli_commands.api, "call", fake_call)
    assert cli_commands.workflows_publish("https://api.example.test", "test-flow.yaml") == 0
    out, _ = capsys.readouterr()
    assert calls[-1] == ("POST", "/api/agent/designer/workflows/test-flow.yaml/publish", {})
    assert "Published test-flow.yaml as v2" in out

    assert cli_commands.workflows_draft_diff("https://api.example.test", "test-flow.yaml") == 0
    out, _ = capsys.readouterr()
    assert calls[-1] == ("GET", "/api/agent/designer/workflows/test-flow.yaml/draft/diff", None)
    assert "identical to live" in out

    assert cli_commands.workflows_discard("https://api.example.test", "test-flow.yaml",
                                          assume_yes=True) == 0
    out, _ = capsys.readouterr()
    assert calls[-1] == ("DELETE", "/api/agent/designer/workflows/test-flow.yaml/draft", None)
    assert "Discarded the draft of test-flow.yaml" in out


def test_cli_discard_prompts_and_respects_no(monkeypatch, capsys):
    calls = []
    monkeypatch.setattr(cli_commands.api, "call",
                        lambda api_url, method, path, body=None, **kwargs:
                        calls.append((method, path)) or {"file": "test-flow.yaml"})
    answers = iter(["n"])
    monkeypatch.setattr("builtins.input", lambda prompt="": next(answers))
    assert cli_commands.workflows_discard("https://api.example.test", "test-flow.yaml") == 1
    assert calls == []
    answers = iter(["y"])
    monkeypatch.setattr("builtins.input", lambda prompt="": next(answers))
    assert cli_commands.workflows_discard("https://api.example.test", "test-flow.yaml") == 0
    assert calls == [("DELETE", "/api/agent/designer/workflows/test-flow.yaml/draft")]


def test_cli_save_reports_the_draft(monkeypatch, capsys, tmp_path):
    def fake_call(api_url, method, path, body=None, **kwargs):
        return {"file": "test-flow.yaml", "published": False,
                "draft": {"base_revision": 0, "stale": False}}

    monkeypatch.setattr(cli_commands.api, "call", fake_call)
    workflow_file = tmp_path / "test-flow.yaml"
    workflow_file.write_text(WORKFLOW_YAML)
    assert cli_commands.workflows_save("https://api.example.test", str(workflow_file),
                                       None) == 0
    out, _ = capsys.readouterr()
    assert "Saved test-flow.yaml as a draft" in out
    assert "workflows publish test-flow.yaml" in out


def test_cli_versions_prints_the_draft_block(monkeypatch, capsys):
    def fake_call(api_url, method, path, body=None, **kwargs):
        return {"workflow": "test-flow", "revision": 2,
                "draft": {"base_revision": 1, "stale": True,
                          "updated_at": "2026-09-28T10:00:00+00:00",
                          "drafted_by": "op-2"},
                "versions": [{"revision": 2, "published_at": "2026-09-28T09:00:00+00:00",
                              "cause": "toggle", "published_by": "op-3",
                              "enabled": False, "current": True}]}

    monkeypatch.setattr(cli_commands.api, "call", fake_call)
    assert cli_commands.workflows_versions("https://api.example.test", "test-flow.yaml") == 0
    out, _ = capsys.readouterr()
    assert "draft: based on v1 by op-2 (stale" in out
