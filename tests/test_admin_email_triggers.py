"""The console's /api/admin/email-triggers endpoint over the shared domain code."""
import json
import unittest
from unittest.mock import patch

from src.dapier.api.admin import routes


def request(method, body=None, query=None):
    return {
        "requestContext": {"http": {"method": method}},
        "queryStringParameters": query,
        "body": json.dumps(body) if body is not None else None,
    }


class AdminEmailTriggerRouteTests(unittest.TestCase):
    def test_list_is_the_workflow_inventory(self):
        payload = {"domain": "dtcdev.click", "addresses": [{"name": "invoice", "handlers": []}]}
        with patch("src.dapier.triggers.email_routes.inventory", return_value=payload):
            listed = routes.list_email_triggers(request("GET"))
        self.assertEqual(json.loads(listed["body"]), payload)
