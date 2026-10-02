"""Stored Telegram workflows are discoverable with the same run identity."""
from dapier_cli import main
from dapier_cli.commands import workflows as cli_workflows
from src.dapier.api import designer_store, overview
from src.dapier.auth.visibility import Visibility
from src.dapier.triggers import hook_triggers, published_workflows, failure_counts


def setup_hooks(monkeypatch):
    monkeypatch.setenv("HOOK_TRIGGERS_TABLE", "hooks")
    monkeypatch.setattr(published_workflows, "configured", lambda: False)
    monkeypatch.setattr(failure_counts, "all_counts", lambda: {})
    monkeypatch.setattr(hook_triggers, "load_items", lambda: [{
        "kind": "telegram", "hook_id": "automator-telegram", "enabled": False,
        "created_by": "alice", "token": "never-list-this",
        "actions": [{"id": "agent", "type": "agent"}],
    }])


def test_console_and_cli_catalog_keep_disabled_telegram_and_its_run_id(monkeypatch, capsys):
    setup_hooks(monkeypatch)
    rows = overview._workflows()
    assert rows[0]["id"] == "telegram-trigger-automator-telegram"
    assert rows[0]["enabled"] is False
    assert rows[0]["source"] is None
    assert rows[0]["trigger"]["connector"] == "telegram"
    assert rows[0]["triggerCount"] == 3
    status, data = designer_store.api_list(q="telegram")
    assert status == 200
    assert data["workflows"][0]["id"] == rows[0]["id"]
    assert "never-list-this" not in str(rows) + str(data)
    assert designer_store.api_list(q="missing")[1]["workflows"] == []
    monkeypatch.setattr(cli_workflows.api, "call", lambda *args, **kwargs: data)
    assert main.main(["workflows", "list"]) == 0
    output = capsys.readouterr().out
    assert rows[0]["id"] in output
    assert "Off" in output


def test_hook_listing_respects_creator_visibility(monkeypatch):
    setup_hooks(monkeypatch)
    visible = Visibility(subject="bob", is_operator=False)
    assert overview._workflows(visible=visible) == []
    assert designer_store.api_list(visible=visible)[1]["workflows"] == []
