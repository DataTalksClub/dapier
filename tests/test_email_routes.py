"""Workflow-owned email inventory, conflict checks, and migration safety."""
import copy
import json

import pytest
import yaml

from dapier_cli import commands, main
from src.dapier.api import agent, designer_store
from src.dapier.api.admin import routes
from src.dapier.engine import matching
from src.dapier.triggers import email_routes, email_triggers, published_workflows


class Table:
    def __init__(self, key):
        self.key, self.items = key, {}

    def put_item(self, Item, **kwargs):
        self.items[Item[self.key]] = copy.deepcopy(Item)

    def get_item(self, Key, **kwargs):
        return {"Item": copy.deepcopy(self.items.get(Key[self.key]))}

    def scan(self, **kwargs):
        assert kwargs.get("ConsistentRead") is True
        return {"Items": copy.deepcopy(list(self.items.values()))}

    def delete_item(self, Key, **kwargs):
        self.items.pop(Key[self.key], None)


@pytest.fixture
def stores(monkeypatch):
    live, old = Table("workflow_id"), Table("name")
    monkeypatch.setenv(published_workflows.TABLE_ENV, "published")
    monkeypatch.setenv(email_triggers.TABLE_ENV, "legacy-email")
    monkeypatch.delenv(designer_store.TOKEN_SECRET_ENV, raising=False)
    monkeypatch.setattr(published_workflows, "get_table", lambda table_ref=None: live)
    monkeypatch.setattr(email_triggers, "get_table", lambda table_ref=None: old)
    monkeypatch.setattr(designer_store, "_sync_youtube", lambda **kwargs: [])
    return live, old


def workflow(name, route="invoice", **extra):
    return {"id": name, "enabled": True,
            "actions": [{"id": "send", "type": "webhook", "url": "https://example.test"}],
            "trigger": {"connector": "email", "event": "message.received",
                        "filters": {"route": route if isinstance(route, dict) else {"equals": route}}}, **extra}


def test_inventory_groups_handlers_and_keeps_patterns_feedback_and_drafts_apart(stores):
    live, _ = stores
    published_workflows.publish(workflow("one", {"in": ["invoice", "receipts"]}), operator="op")
    published_workflows.publish(workflow("two", allow_email_overlap=True), operator="op")
    broad = workflow("all-mail")
    broad["trigger"]["filters"] = {}
    published_workflows.publish(broad)
    watcher = workflow("bounces")
    watcher["trigger"]["event"] = "bounce.received"
    published_workflows.publish(watcher)
    published_workflows.save_draft(workflow("one", "changed"), base_revision=1)
    published_workflows.save_draft(workflow("draft-only", "new-address"), base_revision=0)
    data = email_routes.inventory()
    addresses = {r["name"]: r for r in data["addresses"]}
    assert set(addresses) == {"invoice", "receipts", "new-address"}
    assert [h["workflow"] for h in addresses["invoice"]["handlers"]] == ["one", "two"]
    assert addresses["invoice"]["handlers"][0]["has_draft"] is True
    assert addresses["invoice"]["handlers"][0]["updated_at"]
    assert addresses["new-address"]["handlers"][0]["status"] == "draft"
    assert data["subscriptions"][0]["workflow"] == "all-mail"
    assert data["watchers"][0]["workflow"] == "bounces"


def test_publish_refuses_overlap_but_save_is_only_a_draft(stores):
    published_workflows.publish(workflow("first"))
    status, result = designer_store.api_save({"yaml": yaml.safe_dump(workflow("second"))}, operator="op")
    assert status == 200 and result["published"] is False
    status, result = designer_store.api_publish("second.yaml", operator="op")
    assert status == 409 and "first" in result["error"]
    assert published_workflows.get_item("second") is None
    assert published_workflows.get_draft("second") is not None
    assert designer_store.api_save({"yaml": yaml.safe_dump(workflow("second", allow_email_overlap=True))}, operator="op")[0] == 200
    assert designer_store.api_publish("second.yaml", operator="op")[0] == 200


@pytest.mark.parametrize("route", [{"in": ["invoice", "other"]}, {"prefix": "inv"}, {}])
def test_finite_and_broad_rules_cannot_silently_overlap(stores, route):
    published_workflows.publish(workflow("first"))
    candidate = workflow("second", route)
    if not route:
        candidate["trigger"]["filters"] = {}
    with pytest.raises(email_routes.RouteConflict, match="first"):
        email_routes.validate_ownership(candidate)


def test_disjoint_rules_self_edits_and_renames_are_allowed(stores):
    published_workflows.publish(workflow("first"))
    email_routes.validate_ownership(workflow("other", {"prefix": "todo"}))
    email_routes.validate_ownership(workflow("first"))
    email_routes.validate_ownership(workflow("renamed"), rename_from="first.yaml")


def test_legacy_claims_also_block_workflow_publish(stores):
    _, old = stores
    old.put_item(Item={"name": "invoice", "enabled": True, "actions": workflow("x")["actions"]})
    with pytest.raises(email_routes.RouteConflict, match="email-trigger-invoice"):
        email_routes.validate_ownership(workflow("new"))


def legacy(old, enabled=True, event=None):
    item = {"name": "dropbox-inbox", "enabled": enabled, "description": "Save attachments",
            "address": "dropbox-inbox@dtcdev.click", "updated_at": "2026-10-02T10:00:00Z",
            "filters": {"subject": {"contains": "invoice"}},
            "actions": [{"type": "dropbox_upload", "connection_id": "dropbox", "folder": "email-attachments"}]}
    if event:
        item.update(event=event, address="", filters={"bounce_type": {"equals": "Permanent"}})
    old.put_item(Item=item)


@pytest.mark.parametrize("enabled,event", [(True, None), (False, None), (True, "bounce.received")])
def test_migration_preserves_identity_filters_state_and_actions(stores, enabled, event):
    _, old = stores
    legacy(old, enabled, event)
    status, result = email_routes.migrate("dropbox-inbox", "op")
    assert status == 200 and result["published"] is True
    assert old.items == {}
    item = published_workflows.get_item("email-trigger-dropbox-inbox")
    wf = item["workflow"]
    assert wf["enabled"] is enabled
    assert wf["actions"][0]["id"] == "0"  # preserves execution leases across conversion
    assert wf["actions"][0]["type"] == "dropbox_upload"
    assert wf["trigger"]["event"] == (event or "message.received")
    assert wf["trigger"]["filters"] == ({"bounce_type": {"equals": "Permanent"}} if event else {
        "route": {"equals": "dropbox-inbox"}, "subject": {"contains": "invoice"}})
    entries = matching.all_workflows()
    assert [w["id"] for w in entries] == ["email-trigger-dropbox-inbox"]


def test_partial_migration_can_be_retried_without_double_execution(stores, monkeypatch):
    _, old = stores
    legacy(old)
    delete = old.delete_item
    monkeypatch.setattr(old, "delete_item", lambda **kwargs: (_ for _ in ()).throw(RuntimeError("interrupted")))
    with pytest.raises(RuntimeError):
        email_routes.migrate("dropbox-inbox", "op")
    assert len(matching.all_workflows()) == 1
    assert len(email_routes.inventory()["addresses"][0]["handlers"]) == 1
    monkeypatch.setattr(old, "delete_item", delete)
    assert email_routes.migrate("dropbox-inbox", "op")[0] == 200
    assert old.items == {}


def test_migration_wont_replace_an_existing_workflow_or_draft(stores):
    _, old = stores
    legacy(old)
    wf = workflow("email-trigger-dropbox-inbox", "different")
    published_workflows.save_draft(wf, base_revision=0)
    assert email_routes.migrate("dropbox-inbox", "op")[0] == 409
    published_workflows.delete_draft(wf["id"])
    published_workflows.publish(wf)
    assert email_routes.migrate("dropbox-inbox", "op")[0] == 409
    assert old.items


def test_admin_and_agent_migration_use_the_same_behavior(stores, monkeypatch):
    _, old = stores
    monkeypatch.setattr(agent, "authenticate", lambda event: ("op", None))
    monkeypatch.setattr(agent.authz, "is_operator", lambda *args: True)
    legacy(old)
    event = {"headers": {}, "body": json.dumps({"name": "dropbox-inbox"})}
    assert agent.email_triggers_api(event, "POST")["statusCode"] == 200
    legacy(old)
    assert routes.migrate_email_trigger(event, "op")["statusCode"] == 200


def test_migration_requires_operator(monkeypatch):
    monkeypatch.setattr(agent, "authenticate", lambda event: ("user", None))
    monkeypatch.setattr(agent.authz, "is_operator", lambda *args: False)
    assert agent.email_triggers_api({"headers": {}, "body": '{}'}, "POST")["statusCode"] == 403


def test_cli_can_show_workflow_addresses_and_migrate_through_api(monkeypatch, capsys):
    calls = []
    def call(url, method, path, body=None, **kwargs):
        calls.append((method, path, body))
        if method == "POST":
            return {"workflow": "email-trigger-dropbox-inbox"}
        return {"addresses": [{"name": "invoice", "address": "invoice@dtcdev.click", "handlers": [{
            "workflow": "invoice-intake", "status": "enabled", "action_types": ["dataops"]}]}]}
    monkeypatch.setattr(commands.api, "call", call)
    assert commands.triggers_show("https://example.test", "invoice@dtcdev.click") == 0
    assert "invoice-intake" in capsys.readouterr().out
    assert main.main(["emails", "migrate", "dropbox-inbox"]) == 0
    assert calls[-1] == ("POST", "/api/agent/email-triggers/migrate", {"name": "dropbox-inbox"})


def test_duplicate_requires_explicit_email_fanout(stores):
    published_workflows.publish(workflow("first"))
    status, result = designer_store.api_duplicate("first.yaml", {})
    assert status == 409 and "allow_email_overlap" in result["error"]
    assert published_workflows.get_item("first-copy") is None
