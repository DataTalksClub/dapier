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
    def test_save_list_delete_round_trip(self):
        stub = StubTable()
        with patch.dict(os.environ, {"EMAIL_TRIGGERS_TABLE": "triggers"}), \
                patch.object(email_triggers, "get_table", return_value=stub):
            saved = routes.save_email_trigger(request("PUT", BODY), "op")
            self.assertEqual(saved["statusCode"], 200)
            self.assertTrue(json.loads(saved["body"])["created"])

            listed = json.loads(routes.list_email_triggers(request("GET"))["body"])
            self.assertEqual([t["name"] for t in listed["triggers"]], ["invoice"])
            self.assertEqual(listed["triggers"][0]["address"], "invoice@dtcdev.click")

            deleted = routes.delete_email_trigger(
                request("DELETE", query={"name": "invoice"}), "op")
            self.assertEqual(deleted["statusCode"], 200)
        self.assertEqual(stub.items, {})

    def test_update_keeps_created_provenance(self):
        stub = StubTable()
        with patch.dict(os.environ, {"EMAIL_TRIGGERS_TABLE": "triggers"}), \
                patch.object(email_triggers, "get_table", return_value=stub):
            routes.save_email_trigger(request("PUT", BODY), "op1")
            update = dict(BODY, enabled=False, description="changed")
            updated = json.loads(routes.save_email_trigger(request("PUT", update), "op2")["body"])
        self.assertFalse(updated["created"])
        self.assertFalse(updated["enabled"])
        self.assertEqual(updated["description"], "changed")

    def test_infrastructure_names_stay_rejected(self):
        stub = StubTable()
        with patch.dict(os.environ, {"EMAIL_TRIGGERS_TABLE": "triggers"}), \
                patch.object(email_triggers, "get_table", return_value=stub):
            response = routes.save_email_trigger(
                request("PUT", dict(BODY, name="postmaster")), "op")
        self.assertEqual(response["statusCode"], 400)
        self.assertIn("reserved", json.loads(response["body"])["error"])

    def test_route_claimed_by_yaml_is_rejected(self):
        stub = StubTable()
        with patch.dict(os.environ, {"EMAIL_TRIGGERS_TABLE": "triggers"}), \
                patch.object(email_triggers, "get_table", return_value=stub):
            response = routes.save_email_trigger(
                request("PUT", dict(BODY, name="invoice-attachment")), "op")
        self.assertEqual(response["statusCode"], 400)

    def test_delete_missing_is_404(self):
        stub = StubTable()
        with patch.dict(os.environ, {"EMAIL_TRIGGERS_TABLE": "triggers"}), \
                patch.object(email_triggers, "get_table", return_value=stub):
            response = routes.delete_email_trigger(
                request("DELETE", query={"name": "nope"}), "op")
        self.assertEqual(response["statusCode"], 404)


if __name__ == "__main__":
    unittest.main()
