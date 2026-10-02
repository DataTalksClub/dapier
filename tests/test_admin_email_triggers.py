"""The console's /api/admin/email-triggers endpoints over the shared domain code."""
import json
import os
import unittest
from unittest.mock import patch

from src.dapier.api.admin import routes
from src.dapier.triggers import email_triggers


BODY = {
    "name": "invoice",
    "description": "Save the attachment to Dropbox, then push to the DataOps intake",
    "enabled": True,
    "actions": [
        {"type": "dropbox_upload", "connection_id": "dropbox",
         "source": "attachment", "folder": "_dtc_paperwork/income-invoices"},
        {"type": "dataops", "auth_secret_id": "dapier/dataops",
         "url_env": "DATAOPS_INTAKE_URL"},
    ],
}


def request(method, body=None, query=None):
    return {
        "requestContext": {"http": {"method": method}},
        "queryStringParameters": query,
        "body": json.dumps(body) if body is not None else None,
    }


class StubTable:
    def __init__(self, items=()):
        self.items = {item["name"]: dict(item) for item in items}

    def get_item(self, Key):
        return {"Item": self.items.get(Key["name"])}

    def put_item(self, Item):
        self.items[Item["name"]] = dict(Item)

    def delete_item(self, Key):
        self.items.pop(Key["name"], None)

    def scan(self, **_):
        return {"Items": list(self.items.values())}


class AdminEmailTriggerRouteTests(unittest.TestCase):
    def test_legacy_writes_are_retired_without_touching_storage(self):
        with patch.object(email_triggers, "get_table") as table:
            for response in (routes.save_email_trigger(request("PUT", BODY), "op"),
                             routes.delete_email_trigger(request("DELETE", query={"name": "invoice"}), "op")):
                self.assertEqual(response["statusCode"], 410)
                self.assertIn("workflow", json.loads(response["body"])["error"].lower())
            table.assert_not_called()

    def test_list_is_the_workflow_inventory(self):
        payload = {"domain": "dtcdev.click", "addresses": [{"name": "invoice", "handlers": []}]}
        with patch("src.dapier.triggers.email_routes.inventory", return_value=payload):
            listed = routes.list_email_triggers(request("GET"))
        self.assertEqual(json.loads(listed["body"]), payload)

    def test_migration_uses_shared_domain_behavior(self):
        with patch("src.dapier.triggers.email_routes.migrate", return_value=(200, {"migrated": True})) as migrate:
            response = routes.migrate_email_trigger(request("POST", {"name": "invoice"}), "op")
        self.assertEqual(response["statusCode"], 200)
        migrate.assert_called_once_with("invoice", "op")

    def test_migration_rejects_non_object_body(self):
        self.assertEqual(routes.migrate_email_trigger(request("POST", []), "op")["statusCode"], 400)
