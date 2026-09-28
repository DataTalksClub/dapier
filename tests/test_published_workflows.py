"""Instant publish: the published-workflows store, engine merge, toggle, and CLI."""

import datetime

import pytest
import yaml

from src.dapier.api import designer_store
from src.dapier.engine import matching
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


def seeded_workflow(**overrides):
    workflow = {
        "id": "test-flow",
        "enabled": True,
        "trigger": {"connector": "email", "event": "message.received"},
        "actions": [{"id": "a1", "type": "webhook", "url": "https://example.test/hook"}],
    }
    workflow.update(overrides)
    return workflow


def test_publish_stores_a_scrubbable_workflow(published):
    workflow = seeded_workflow(
        note=None,
        day=datetime.date(2026, 9, 25),
        filters={"a": None, "b": {"equals": 1}},
    )
    item = published_workflows.publish(workflow, operator="op")
    stored = item["workflow"]
    assert "note" not in stored
    assert stored["day"] == "2026-09-25"
    assert "a" not in stored["filters"]
    assert published_workflows.get_item("test-flow")["enabled"] is True


def test_load_workflows_roundtrips_the_definition(published):
    published_workflows.publish(seeded_workflow())
    assert published_workflows.load_workflows() == [seeded_workflow()]


def test_save_publishes_live(github_ready, published):
    status, payload = designer_store.api_save({"yaml": WORKFLOW_YAML}, operator="op")
    assert status == 200
    assert payload["published"] is True
    item = published.items["test-flow"]
    assert item["enabled"] is True
    assert item["published_by"] == "op"


def test_save_requires_publish_table(github_ready, monkeypatch):
    monkeypatch.delenv(published_workflows.TABLE_ENV, raising=False)
    status, payload = designer_store.api_save({"yaml": WORKFLOW_YAML})
    assert status == 503
    assert "not configured" in payload["error"]


def test_save_without_git_sync_publishes(published, monkeypatch):
    monkeypatch.delenv(designer_store.TOKEN_SECRET_ENV, raising=False)
    status, payload = designer_store.api_save({"yaml": WORKFLOW_YAML})
    assert status == 200
    assert payload["published"] is True
    assert payload["revision"] == 1
    assert "commit" not in payload


def test_save_publish_failure_is_loud(github_ready, published, monkeypatch):
    def boom(*args, **kwargs):
        raise RuntimeError("dynamo down")

    monkeypatch.setattr(published_workflows, "publish", boom)
    status, payload = designer_store.api_save({"yaml": WORKFLOW_YAML})
    assert status == 502
    assert "dynamo down" in payload["error"]
    assert "test-flow" not in published.items


def test_save_rename_unpublishes_the_old_id(github_ready, published):
    published_workflows.publish(seeded_workflow(id="old-name"))
    status, payload = designer_store.api_save(
        {"yaml": WORKFLOW_YAML, "renameFrom": "old-name.yaml"}, operator="op")
    assert status == 200
    assert "test-flow" in published.items
    assert "old-name" not in published.items


def test_toggle_publishes_and_commits(github_ready, published):
    published_workflows.publish(seeded_workflow())
    status, payload = designer_store.api_toggle(
        "test-flow.yaml", {"enabled": False}, operator="op")
    assert status == 200
    assert payload["enabled"] is False
    assert payload["published"] is True
    assert payload["commit"] == "commit456"
    assert published.items["test-flow"]["enabled"] is False


def test_toggle_commits_actions_before_trigger(github_ready, published, monkeypatch):
    # The toggle commits the stored dict (here: trigger before actions); the
    # dumped YAML still gets the canonical order the designer writes.
    published_workflows.publish(seeded_workflow())
    captured = {}

    def github(method, path, token, payload=None):
        if method == "POST" and path.endswith("/git/trees"):
            captured["blob"] = payload["tree"][0]["content"]
        return GITHUB_SCRIPT[(method, path)]

    monkeypatch.setattr(designer_store, "_github", github)
    status, _ = designer_store.api_toggle("test-flow.yaml", {"enabled": False})
    assert status == 200
    assert list(yaml.safe_load(captured["blob"])) == ["id", "enabled", "actions", "trigger"]


def test_toggle_survives_a_git_failure(github_ready, published, monkeypatch):
    def refuse(method, path, token, payload=None):
        raise designer_store.SyncError("github down")

    monkeypatch.setattr(designer_store, "_github", refuse)
    published_workflows.publish(seeded_workflow())
    status, payload = designer_store.api_toggle("test-flow.yaml", {"enabled": False})
    assert status == 200
    assert "git_sync_error" in payload
    assert published.items["test-flow"]["enabled"] is False


def test_toggle_validates_input(published):
    assert designer_store.api_toggle("test-flow.yaml", {})[0] == 400
    assert designer_store.api_toggle("test-flow.yaml", {"enabled": "yes"})[0] == 400
    assert designer_store.api_toggle("../etc", {"enabled": True})[0] == 400


def test_toggle_unconfigured_is_503(github_ready, monkeypatch):
    monkeypatch.delenv(published_workflows.TABLE_ENV, raising=False)
    assert designer_store.api_toggle("test-flow.yaml", {"enabled": False})[0] == 503


def test_toggle_unknown_workflow_is_404(published, monkeypatch, tmp_path):
    monkeypatch.setenv("WORKFLOWS_DIR", str(tmp_path))  # empty bundle, nothing published
    monkeypatch.delenv(designer_store.TOKEN_SECRET_ENV, raising=False)  # nowhere to fall back
    status, payload = designer_store.api_toggle("test-flow.yaml", {"enabled": False})
    assert status == 404


def test_api_list_uses_published_state(github_ready, published, monkeypatch, tmp_path):
    monkeypatch.setenv("WORKFLOWS_DIR", str(tmp_path))
    (tmp_path / "bundled-flow.yaml").write_text(
        "id: bundled-flow\nenabled: true\n"
        "trigger:\n  connector: email\n  event: message.received\n"
        "actions:\n  - type: webhook\n    url: https://example.test/hook\n")
    published_workflows.publish(seeded_workflow(enabled=False, id="bundled-flow"))
    published_workflows.publish(seeded_workflow(id="cloud-only"))

    status, payload = designer_store.api_list()
    items = {item["id"]: item for item in payload["workflows"]}
    assert items["bundled-flow"]["published"] is True
    assert items["bundled-flow"]["enabled"] is False
    assert items["cloud-only"]["published"] is True
    assert "deployed" not in items["cloud-only"]


def test_api_get_prefers_the_published_state(published, monkeypatch, tmp_path):
    monkeypatch.setenv("WORKFLOWS_DIR", str(tmp_path))
    (tmp_path / "test-flow.yaml").write_text(
        WORKFLOW_YAML.replace("enabled: true", "enabled: false"))
    published_workflows.publish(seeded_workflow())
    status, payload = designer_store.api_get("test-flow.yaml")
    assert status == 200
    assert payload["published"] is True
    assert payload["workflow"]["enabled"] is True


def test_engine_reads_published_workflows(published, monkeypatch, tmp_path):
    monkeypatch.setenv("WORKFLOWS_DIR", str(tmp_path))
    (tmp_path / "test-flow.yaml").write_text(WORKFLOW_YAML)
    published_workflows.publish(seeded_workflow(enabled=False))

    merged = {workflow["id"]: workflow for workflow in matching.all_workflows()}
    assert merged["test-flow"]["enabled"] is False
    event = {"connector": "email", "event": "message.received", "data": {}}
    assert matching.matches(merged["test-flow"], event) is False


def test_engine_has_no_workflows_when_publish_table_absent(monkeypatch, tmp_path):
    monkeypatch.delenv(published_workflows.TABLE_ENV, raising=False)
    monkeypatch.setenv("WORKFLOWS_DIR", str(tmp_path))
    (tmp_path / "test-flow.yaml").write_text(WORKFLOW_YAML)
    merged = {workflow["id"]: workflow for workflow in matching.all_workflows()}
    assert merged == {}


def test_cli_on_and_off_use_the_bulk_endpoint(monkeypatch, capsys):
    from dapier_cli import commands as cli_commands

    calls = []

    def fake_call(api_url, method, path, body=None, debug=False):
        calls.append((method, path, body))
        return {"results": [{"file": "test-flow.yaml", "ok": True,
                             "enabled": body["action"] == "enable",
                             "commit": "commit456"}]}

    monkeypatch.setattr(cli_commands.api, "call", fake_call)
    assert cli_commands.workflows_set_enabled("https://api.test", "test-flow.yaml", True) == 0
    assert cli_commands.workflows_set_enabled("https://api.test", "test-flow.yaml", False) == 0
    assert calls[0] == ("POST", "/api/agent/designer/workflows/bulk",
                        {"ids": ["test-flow.yaml"], "action": "enable"})
    assert calls[1] == ("POST", "/api/agent/designer/workflows/bulk",
                        {"ids": ["test-flow.yaml"], "action": "disable"})
    out = capsys.readouterr().out
    assert "test-flow.yaml is On" in out and "test-flow.yaml is Off" in out

    from dapier_cli.main import build_parser, cmd_workflows
    parser = build_parser()
    for command in ("on", "off", "enable", "disable"):
        args = parser.parse_args(["workflows", command, "test-flow.yaml"])
        assert (args.group, args.command, args.file) == ("workflows", command, ["test-flow.yaml"])
        assert cmd_workflows(args, "https://api.test", False) == 0
    assert [body["action"] for _, _, body in calls[2:]] == [
        "enable", "disable", "enable", "disable"]


def test_cli_bulk_reports_per_workflow_failures(monkeypatch, capsys):
    from dapier_cli import commands as cli_commands

    monkeypatch.setattr(
        cli_commands.api, "call",
        lambda api_url, method, path, body=None, debug=False: {
            "results": [
                {"file": "went.yaml", "ok": True},
                {"file": "stuck.yaml", "ok": False, "error": "still parked on a delay"},
            ]})
    assert cli_commands.workflows_set_enabled("https://api.test", "stuck.yaml", False) == 0
    assert "stuck.yaml: still parked on a delay" in capsys.readouterr().out


# ---- Version history and rollback ----

def test_publish_records_a_version_history(published):
    previous = published_workflows.publish(seeded_workflow(), operator="op-1")
    published_workflows.publish(
        seeded_workflow(actions=[{"id": "a1", "type": "slack", "text": "hi"}]),
        operator="op-2", previous=previous,
    )
    versions = published_workflows.list_versions("test-flow")
    assert [version["revision"] for version in versions] == [2, 1]
    assert [version["published_by"] for version in versions] == ["op-2", "op-1"]
    assert versions[0]["workflow"]["actions"][0]["type"] == "slack"
    assert versions[0]["cause"] == "save"
    assert published_workflows.get_version("test-flow", 1)["published_by"] == "op-1"
    assert published_workflows.get_version("test-flow", 9) is None
    assert published_workflows.get_item("test-flow")["revision"] == 2


def test_version_records_stay_hidden_from_the_engine(published):
    published_workflows.publish(seeded_workflow(), operator="op")
    assert [item["workflow_id"] for item in published_workflows.load_items()] == ["test-flow"]
    assert published_workflows.load_workflows() == [seeded_workflow()]


def test_version_history_is_pruned(published):
    previous = {}
    for _ in range(published_workflows.MAX_VERSIONS + 5):
        previous = published_workflows.publish(seeded_workflow(), previous=previous, operator="op")
    revisions = [version["revision"] for version in published_workflows.list_versions("test-flow")]
    assert revisions == list(range(published_workflows.MAX_VERSIONS + 5, 5, -1))
    assert published_workflows.get_item("test-flow")["revision"] \
        == published_workflows.MAX_VERSIONS + 5


def test_rollback_restores_the_old_definition(published, github_ready):
    published_workflows.publish(seeded_workflow(), operator="op-1")
    designer_store.api_toggle("test-flow.yaml", {"enabled": False}, operator="op-2")
    status, payload = designer_store.api_rollback(
        "test-flow.yaml", {"revision": 1}, operator="op-3")
    assert status == 200
    assert payload["published"] is True
    causes = {version["revision"]: version["cause"]
              for version in published_workflows.list_versions("test-flow")}
    assert causes == {1: "save", 2: "toggle", 3: "rollback"}
    live = published_workflows.get_item("test-flow")
    assert live["revision"] == 3
    assert live["enabled"] is True  # v1's flag came back with the definition
    assert live["workflow"] == published_workflows.get_version("test-flow", 1)["workflow"]
    assert live["published_by"] == "op-3"


def test_versions_lists_the_live_revision(published, github_ready):
    published_workflows.publish(seeded_workflow(), operator="op-1")
    status, payload = designer_store.api_versions("test-flow.yaml")
    assert status == 200
    assert payload["workflow"] == "test-flow"
    assert payload["revision"] == 1
    assert len(payload["versions"]) == 1
    assert payload["versions"][0]["current"] is True


def test_rollback_validates_input(published, github_ready):
    published_workflows.publish(seeded_workflow(), operator="op-1")
    # An omitted revision means "the version before the live one"; at v1 there is none.
    assert designer_store.api_rollback("test-flow.yaml", {})[0] == 409
    assert designer_store.api_rollback("test-flow.yaml", {"revision": "x"})[0] == 400
    assert designer_store.api_rollback("test-flow.yaml", {"revision": True})[0] == 400
    assert designer_store.api_rollback("test-flow.yaml", {"revision": 7})[0] == 404
    # From v2, the revision-less form restores v1.
    status, payload = designer_store.api_save(
        {"yaml": yaml.safe_dump(
            seeded_workflow(actions=[{"id": "a1", "type": "slack", "text": "hi"}]))},
        operator="op-2")
    assert status == 200
    status, payload = designer_store.api_rollback("test-flow.yaml", {}, operator="op-3")
    assert status == 200 and payload["published"] is True
    live = published_workflows.get_item("test-flow")
    assert live["revision"] == 3
    assert live["workflow"]["actions"][0]["type"] == "webhook"


def test_versions_and_rollback_unconfigured_are_503(monkeypatch):
    monkeypatch.delenv(published_workflows.TABLE_ENV, raising=False)
    assert designer_store.api_versions("test-flow.yaml")[0] == 503
    assert designer_store.api_rollback("test-flow.yaml", {"revision": 1})[0] == 503


def test_list_versions_caps_the_scan_and_keeps_newest_first(published, monkeypatch):
    """The version scan is bounded like load_items (Limit=SCAN_LIMIT): the
    newest revisions within the window come back, sorted desc, and the live
    items sharing the table are filtered out."""
    class RecordingTable:
        def __init__(self, items):
            self.items = items
            self.scan_limits = []

        def scan(self, **kwargs):
            self.scan_limits.append(kwargs.get("Limit"))
            return {"Items": list(self.items)}

    items = [
        {"workflow_id": f"test-flow#v{rev}", "version_of": "test-flow",
         "revision": rev, "published_at": f"2026-09-0{rev}T00:00:00+00:00"}
        # Out of order on purpose: the sort, not the scan order, decides.
        for rev in (3, 1, 4, 2)
    ]
    items.append({"workflow_id": "other-flow", "revision": 9})  # live item, not a version
    table = RecordingTable(items)
    monkeypatch.setattr(published_workflows, "get_table", lambda table_ref=None: table)

    versions = published_workflows.list_versions("test-flow", table_ref=table)

    assert table.scan_limits == [published_workflows.SCAN_LIMIT]
    assert [version["revision"] for version in versions] == [4, 3, 2, 1]
