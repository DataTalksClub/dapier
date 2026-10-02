"""Hook-backed workflows open in the designer (read-only) and their ids are
save-refused.

A stored webhook/telegram/... trigger projects into an engine workflow listed
beside the managed ones with no source file. The designer and CLI read it
through the same GET the managed store serves — by id — and the write verbs
refuse its id: the engine prefers managed definitions, so a publish under a
trigger's id would silently take over the trigger's routing. Duplicate is the
edit path: it loads through the same read and copies the definition into an
ordinary managed workflow the trigger does not own.
"""

import json

import pytest
import yaml

from src.dapier.api import designer_store, overview
from src.dapier.auth.visibility import Visibility
from src.dapier.triggers import hook_triggers, published_workflows, failure_counts
from dapier_cli import main
from dapier_cli.commands import workflows as cli_workflows


HOOK_ITEM = {
    "kind": "telegram", "hook_id": "todo", "enabled": True,
    "created_by": "alice", "token": "never-leak",
    "actions": [{"id": "code", "type": "code", "code": "return {ok: true}"}],
}
WORKFLOW_ID = "telegram-trigger-todo"


@pytest.fixture
def isolated_home(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.delenv("DAPIER_API_URL", raising=False)
    return tmp_path


class StubTable:
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
def stores(monkeypatch):
    """Both stores configured and stubbed: one telegram hook, an empty
    published table, and no live failure counts."""
    monkeypatch.setenv(published_workflows.TABLE_ENV, "published-test")
    monkeypatch.setenv(hook_triggers.TABLE_ENV, "hooks-test")
    table = StubTable()
    monkeypatch.setattr(published_workflows, "get_table", lambda table_ref=None: table)
    monkeypatch.setattr(hook_triggers, "load_items", lambda: [dict(HOOK_ITEM)])
    monkeypatch.setattr(failure_counts, "all_counts", lambda: {})
    return table


def test_api_get_serves_the_hook_workflow_by_id(stores):
    for ref in (WORKFLOW_ID, f"{WORKFLOW_ID}.yaml"):
        status, data = designer_store.api_get(ref)
        assert status == 200
        assert data["workflow"]["id"] == WORKFLOW_ID
        assert data["workflow"]["enabled"] is True
        assert data["published"] is True
        assert data["hook_backed"] is True
        # Canonical YAML, alias-free: the projected trigger fan-out shares one
        # filters mapping, and the text must stay hand-editable.
        assert "&id0" not in data["yaml"] and "*id0" not in data["yaml"]
        parsed = yaml.safe_load(data["yaml"])
        assert parsed["id"] == WORKFLOW_ID
        assert len(parsed["triggers"]) == 3
    assert designer_store.api_get("telegram-trigger-nope")[0] == 404
    assert "never-leak" not in str(designer_store.api_get(WORKFLOW_ID))


def test_api_get_scopes_the_hook_workflow_by_creator(stores):
    hidden = Visibility(subject="bob", is_operator=False)
    assert designer_store.api_get(WORKFLOW_ID, visible=hidden)[0] == 404
    operator = Visibility(subject="bob", is_operator=True)
    assert designer_store.api_get(WORKFLOW_ID, visible=operator)[0] == 200
    creator = Visibility(subject="alice", is_operator=False)
    assert designer_store.api_get(WORKFLOW_ID, visible=creator)[0] == 200


def test_list_marks_the_hook_row_hook_backed(stores):
    status, data = designer_store.api_list()
    assert status == 200
    rows = {row["id"]: row for row in data["workflows"]}
    assert rows[WORKFLOW_ID]["hook_backed"] is True
    assert rows[WORKFLOW_ID]["source"] is None
    # A managed row carries no flag and keeps its file.
    stores.put_item({"workflow_id": "managed-one", "file": "managed-one.yaml",
                     "workflow": {"id": "managed-one", "trigger": {
                         "connector": "email", "event": "message.received"},
                         "actions": [{"id": "a", "type": "webhook",
                                      "url": "https://example.test"}]},
                     "enabled": True, "revision": 1})
    _, data = designer_store.api_list()
    rows = {row["id"]: row for row in data["workflows"]}
    assert rows["managed-one"].get("hook_backed") is None
    assert rows["managed-one"]["source"] == "managed-one.yaml"


def test_overview_rows_carry_hook_backed(stores):
    rows = {row["id"]: row for row in overview._workflows()}
    assert rows[WORKFLOW_ID]["hook_backed"] is True
    assert rows[WORKFLOW_ID]["source"] is None


def test_save_refuses_the_trigger_owned_id(stores):
    yaml_text = designer_store.api_get(WORKFLOW_ID)[1]["yaml"]
    # A draft: refused, so no draft can later publish over the trigger.
    status, payload = designer_store.api_save({"yaml": yaml_text})
    assert status == 409
    assert "trigger" in payload["error"]
    # Live: refused — this is the takeover the guard exists for.
    status, payload = designer_store.api_save({"yaml": yaml_text}, live=True)
    assert status == 409
    assert WORKFLOW_ID in payload["error"]
    # A different id saves fine.
    status, _ = designer_store.api_save(
        {"yaml": yaml_text.replace(WORKFLOW_ID, "my-todo-copy")})
    assert status == 200


def test_duplicate_by_id_copies_the_hook_workflow(stores):
    status, payload = designer_store.api_duplicate(WORKFLOW_ID)
    assert status == 200
    assert payload["file"] == f"{WORKFLOW_ID}-copy.yaml"
    assert payload["duplicated_from"] == WORKFLOW_ID
    # The copy is an ordinary managed workflow; the hook item is untouched.
    status, data = designer_store.api_get(f"{WORKFLOW_ID}-copy.yaml")
    assert status == 200
    assert data.get("hook_backed") is None
    assert data["workflow"]["id"] == f"{WORKFLOW_ID}-copy"
    assert hook_triggers.load_items()[0]["actions"][0]["id"] == "code"


def test_duplicate_refuses_a_name_owned_by_another_hook(stores, monkeypatch):
    monkeypatch.setattr(hook_triggers, "load_items", lambda: [
        dict(HOOK_ITEM), {
            "kind": "telegram", "hook_id": "automator", "enabled": True,
            "created_by": "alice", "token": "t",
            "actions": [{"id": "code", "type": "code", "code": "return {ok: 1}"}],
        }])
    status, payload = designer_store.api_duplicate(
        WORKFLOW_ID, {"name": "telegram-trigger-automator"})
    assert status == 409
    assert "trigger" in payload["error"]


def test_cli_show_prints_the_hook_workflow(isolated_home, monkeypatch, capsys):
    def fake_call(api_url, method, path, body=None, **kwargs):
        assert (method, path) == ("GET", f"/api/agent/designer/workflows/{WORKFLOW_ID}")
        return {"workflow": {"id": WORKFLOW_ID, "enabled": True},
                "published": True, "hook_backed": True,
                "yaml": f"id: {WORKFLOW_ID}\n"}

    monkeypatch.setattr(cli_workflows.api, "call", fake_call)
    assert main.main(["workflows", "show", WORKFLOW_ID]) == 0
    out = capsys.readouterr().out
    assert json.loads(out)["id"] == WORKFLOW_ID


def test_cli_export_prints_the_hook_yaml(isolated_home, monkeypatch, capsys):
    monkeypatch.setattr(
        cli_workflows.api, "call",
        lambda *args, **kwargs: {"workflow": {"id": WORKFLOW_ID},
                                 "yaml": f"id: {WORKFLOW_ID}\n", "hook_backed": True})
    assert main.main(["workflows", "export", WORKFLOW_ID]) == 0
    assert f"id: {WORKFLOW_ID}" in capsys.readouterr().out


def test_agent_route_serves_a_hook_id_without_a_file_suffix(stores, monkeypatch):
    """The dispatcher's item route used to require ``.yaml`` — a hook id never
    matched, so the designer's open and the CLI show 404ed at the router,
    before the store could answer."""
    from src.dapier.api import agent as agent_api
    from src.dapier.api.agent import designer as agent_designer

    monkeypatch.setattr(agent_designer, "require_operator",
                        lambda event, perm: ("alice", None))
    monkeypatch.setattr(agent_designer, "_visibility",
                        lambda event, subject: Visibility(subject="alice", is_operator=True))
    request = {"headers": {"host": "dapier.example.test"}, "cookies": []}
    response = agent_api.route(request, "GET",
                               f"/api/agent/designer/workflows/{WORKFLOW_ID}")
    assert response["statusCode"] == 200
    body = json.loads(response["body"])
    assert body["workflow"]["id"] == WORKFLOW_ID
    assert body["hook_backed"] is True
    # An unknown id still answers 404 through the same route.
    missing = agent_api.route(request, "GET",
                              "/api/agent/designer/workflows/telegram-trigger-nope")
    assert missing["statusCode"] == 404
