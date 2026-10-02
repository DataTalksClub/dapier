"""Email-trigger vocabulary: name/address rules, action validation, inventory API."""
import json
import unittest
from unittest.mock import patch

from src.dapier.api import agent as agent_api
from src.dapier.triggers import email_triggers


class NameValidationTests(unittest.TestCase):
    def test_accepts_and_normalizes(self):
        self.assertEqual(email_triggers.validate_name(" Income-2026 "), "income-2026")

    def test_rejects_bad_names(self):
        for name in ("", "a_b", "-lead", "trail-", "x" * 40, "do it", "a"):
            with self.assertRaises(email_triggers.TriggerError):
                email_triggers.validate_name(name)

    def test_rejects_reserved(self):
        with self.assertRaises(email_triggers.TriggerError) as ctx:
            email_triggers.validate_name("postmaster")
        self.assertIn("reserved", str(ctx.exception))

    def test_infrastructure_addresses_are_the_only_hard_reserved(self):
        # Routes are otherwise free: ownership is guarded by the workflow
        # publish paths (email_routes.validate_ownership), not by this list.
        self.assertEqual(email_triggers.validate_name("invoice"), "invoice")


class AddressTests(unittest.TestCase):
    def test_address_uses_configured_domain(self):
        with patch.dict("os.environ", {"TRIGGER_EMAIL_DOMAIN": "dtcdev.click"}):
            self.assertEqual(email_triggers.address_for("income-2026-08"),
                             "income-2026-08@dtcdev.click")


class ActionValidationTests(unittest.TestCase):
    def test_requires_at_least_one_action(self):
        with self.assertRaises(email_triggers.TriggerError):
            email_triggers.validate_actions([])

    def test_rejects_unknown_types_and_keys(self):
        with self.assertRaises(email_triggers.TriggerError):
            email_triggers.validate_actions([{"type": "carrier-pigeon"}])
        with self.assertRaises(email_triggers.TriggerError) as ctx:
            email_triggers.validate_actions([{"type": "webhook", "url": "https://x", "bogus": 1}])
        self.assertIn("bogus", str(ctx.exception))

    def test_requires_type_keys(self):
        with self.assertRaises(email_triggers.TriggerError) as ctx:
            email_triggers.validate_actions([{"type": "dropbox_upload", "connection_id": "dropbox"}])
        self.assertIn("folder", str(ctx.exception))

    def test_accepts_email_send_and_rejects_unknown_keys(self):
        actions = [{"type": "email_send", "to": "ops@example.com",
                    "subject": "s", "text": "t", "html": "<b>t</b>", "sender": "me@dtcdev.click"}]
        self.assertEqual(email_triggers.validate_actions(actions), actions)
        with self.assertRaises(email_triggers.TriggerError):
            email_triggers.validate_actions([{"type": "email_send"}])
        with self.assertRaises(email_triggers.TriggerError) as ctx:
            email_triggers.validate_actions(
                [{"type": "email_send", "to": "ops@example.com", "bcc": "x"}])
        self.assertIn("bcc", str(ctx.exception))


class FlowBindingTests(unittest.TestCase):
    """Trigger bodies carry inline actions; flow references are retired."""

    def test_rejects_flow_references(self):
        for body in ({"flow": "invoice-dataops",
                      "actions": [{"type": "webhook", "url": "https://x"}]},
                     {"flow": "nope"}):
            with self.assertRaises(email_triggers.TriggerError) as refused:
                email_triggers.resolve_actions(body)
            self.assertIn("shared flows are retired", str(refused.exception))


class AgentApiTests(unittest.TestCase):
    def test_operator_reads_the_workflow_inventory(self):
        payload = {"domain": "dtcdev.click", "addresses": []}
        with patch("src.dapier.api.agent.authenticate", return_value=("sub-1", None)), \
             patch("src.dapier.api.agent.authz.is_operator", return_value=True), \
             patch("src.dapier.triggers.email_routes.inventory", return_value=payload):
            listed = agent_api.email_triggers_api({"headers": {}}, "GET")
        self.assertEqual(listed["statusCode"], 200)
        self.assertEqual(json.loads(listed["body"]), payload)

    def test_non_operator_is_rejected(self):
        with patch("src.dapier.api.agent.authenticate", return_value=("sub-2", None)), \
             patch("src.dapier.api.agent.authz.is_operator", return_value=False):
            response = agent_api.email_triggers_api({"headers": {}}, "GET")

        self.assertEqual(response["statusCode"], 403)


if __name__ == "__main__":
    unittest.main()
