"""The one-time cutover preserves definitions and stored trigger identities."""

from pathlib import Path

from scripts.migrate_legacy_workflows import catalog, plan, apply


class Table:
    def __init__(self, key, items=()):
        self.key = key
        self.items = {item[key]: dict(item) for item in items}

    def scan(self, **_):
        return {"Items": list(self.items.values())}

    def put_item(self, Item, ConditionExpression=None):
        key = Item[self.key]
        if ConditionExpression and key in self.items:
            raise AssertionError("migration overwrote an existing record")
        self.items[key] = Item

    def update_item(self, Key, UpdateExpression, ConditionExpression,
                    ExpressionAttributeValues):
        item = self.items[Key[self.key]]
        assert item["flow"] == ExpressionAttributeValues[":old"]
        item["actions"] = ExpressionAttributeValues[":actions"]
        item["flow"] = ExpressionAttributeValues[":empty"]

    def delete_item(self, Key):
        self.items.pop(Key[self.key], None)


def test_catalog_inlines_every_legacy_workflow():
    root = Path(__file__).resolve().parents[1] / "migrations/legacy-workflows"
    workflows, flows = catalog(root)
    assert len(workflows) == 10
    assert len(flows) == 5
    assert workflows["todo-intake"]["actions"] == flows["todo-email-sheet"]
    assert all("flow" not in workflow and "flows" not in workflow
               for workflow in workflows.values())


def test_cutover_keeps_newer_published_edits_and_materializes_hooks(tmp_path):
    (tmp_path / "legacy.yaml").write_text(
        "flows:\n  shared:\n    actions: [{id: send, type: webhook, url: https://example.test}]\n"
        "id: legacy\nenabled: true\nflow: shared\n"
        "trigger: {connector: email, event: message.received}\n")
    workflows, flows = catalog(tmp_path)
    existing = {"workflow_id": "legacy", "workflow": {
        "id": "legacy", "enabled": False,
        "trigger": {"connector": "email", "event": "message.received"},
        "actions": [{"id": "newer", "type": "webhook", "url": "https://newer.test"}]}}
    published = Table("workflow_id", [existing])
    hook = Table("hook_id", [{"hook_id": "telegram", "flow": "shared",
                              "actions": [], "token": "kept", "enabled": False}])
    tables = {"workflows": (published, "workflow_id"),
              "hook": (hook, "hook_id"),
              "email": (Table("name"), "name"),
              "schedule": (Table("schedule_id"), "schedule_id"),
              "poll": (Table("poll_id"), "poll_id")}
    missing, published_updates, trigger_updates = plan(workflows, flows, tables)
    assert missing == {}
    assert published_updates == []
    assert len(trigger_updates) == 1
    apply(missing, published_updates, trigger_updates, tables)
    assert published.items["legacy"] == existing
    assert hook.items["telegram"]["token"] == "kept"
    assert hook.items["telegram"]["enabled"] is False
    assert hook.items["telegram"]["flow"] == ""
    assert hook.items["telegram"]["actions"] == flows["shared"]
