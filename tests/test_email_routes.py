"""Workflow-owned email inventory and conflict checks."""
import copy

import pytest
import yaml

from dapier_cli import commands
from src.dapier.api import designer_store
from src.dapier.engine import matching
from src.dapier.triggers import email_routes, published_workflows


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
    live = Table("workflow_id")
    monkeypatch.setenv(published_workflows.TABLE_ENV, "published")
    monkeypatch.delenv(designer_store.TOKEN_SECRET_ENV, raising=False)
    monkeypatch.setattr(published_workflows, "get_table", lambda table_ref=None: live)
    monkeypatch.setattr(designer_store, "_sync_youtube", lambda **kwargs: [])
    return live


def workflow(name, route="invoice", **extra):
    return {"id": name, "enabled": True,
            "actions": [{"id": "send", "type": "webhook", "url": "https://example.test"}],
            "trigger": {"connector": "email", "event": "message.received",
                        "filters": {"route": route if isinstance(route, dict) else {"equals": route}}}, **extra}


def test_inventory_groups_handlers_and_keeps_patterns_feedback_and_drafts_apart(stores):
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


def test_cli_can_show_workflow_addresses(monkeypatch, capsys):
    def call(url, method, path, body=None, **kwargs):
        return {"addresses": [{"name": "invoice", "address": "invoice@dtcdev.click", "handlers": [{
            "workflow": "invoice-intake", "status": "enabled", "action_types": ["dataops"]}]}]}
    monkeypatch.setattr(commands.api, "call", call)
    assert commands.triggers_show("https://example.test", "invoice@dtcdev.click") == 0
    assert "invoice-intake" in capsys.readouterr().out


def test_duplicate_requires_explicit_email_fanout(stores):
    published_workflows.publish(workflow("first"))
    status, result = designer_store.api_duplicate("first.yaml", {})
    assert status == 409 and "allow_email_overlap" in result["error"]
    assert published_workflows.get_item("first-copy") is None


def test_two_matching_triggers_in_one_workflow_show_one_address_owner(stores):
    first = workflow("first", {"in": ["invoice", "receipts"]})
    first["triggers"] = [first.pop("trigger"), {
        "connector": "email", "event": "message.received",
        "filters": {"route": {"equals": "invoice"}, "subject": {"contains": "bill"}}}]
    published_workflows.publish(first)
    rows = {row["name"]: row for row in email_routes.inventory()["addresses"]}
    assert len(rows["invoice"]["handlers"]) == 1
    assert len(rows["invoice"]["handlers"][0]["matching_filters"]) == 2
    assert len(rows["receipts"]["handlers"][0]["matching_filters"]) == 1
    assert matching.matches(first, {"connector": "email", "event": "message.received", "data": {"route": "invoice"}})
