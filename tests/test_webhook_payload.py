"""webhook payload shaping: an optional templated JSON body beside the
whole-event default.

The transport is injected, so every test records the exact body and headers
the runner produced — no network. The signature always covers whatever body
is actually sent, shaped or not.
"""
import hashlib
import hmac
import json
import unittest
from unittest.mock import patch

from src.dapier.connectors import registry
from src.dapier.engine.actions import webhook as webhook_action


def recording_transport(status=200, raw=b'{"ok": true}'):
    calls = []

    def transport(method, url, *, headers=None, body=None, timeout=10):
        calls.append({"method": method, "url": url, "headers": headers,
                      "body": body, "timeout": timeout})
        return status, raw

    transport.calls = calls
    return transport


def run(action, event=None, steps=None, transport=None):
    return webhook_action.run_webhook(action, event or {}, steps=steps,
                                      transport=transport)


EVENT = {"id": "evt-1", "connector": "email",
         "data": {"subject": "invoice", "total": "42.00"}}


class DefaultBodyTests(unittest.TestCase):
    def test_without_a_payload_the_whole_event_is_the_body(self):
        transport = recording_transport()
        run({"type": "webhook", "url": "https://example.test/hook"},
            EVENT, transport=transport)
        expected = json.dumps(EVENT, separators=(",", ":"), sort_keys=True).encode()
        self.assertEqual(transport.calls[0]["body"], expected)

    def test_default_signature_still_signs_the_event_body(self):
        transport = recording_transport()
        action = {"type": "webhook", "url": "https://example.test/hook",
                  "secret_id": "dapier/webhook"}
        with patch("src.dapier.engine.actions.webhook.base._signing_secret",
                   return_value="s3cret"):
            run(action, EVENT, transport=transport)
        expected = hmac.new(b"s3cret", transport.calls[0]["body"], hashlib.sha256).hexdigest()
        self.assertEqual(transport.calls[0]["headers"]["x-dapier-signature"],
                         f"sha256={expected}")


class ShapedPayloadTests(unittest.TestCase):
    def test_payload_sends_exactly_the_rendered_json(self):
        transport = recording_transport()
        action = {"type": "webhook", "url": "https://example.test/hook",
                  "payload": '{"id": "{trigger.id}", "subject": "{trigger.subject}"}'}
        run(action, EVENT, transport=transport)
        expected = json.dumps({"id": "evt-1", "subject": "invoice"},
                              separators=(",", ":"), sort_keys=True).encode()
        self.assertEqual(transport.calls[0]["body"], expected)

    def test_payload_templates_pull_from_the_event(self):
        transport = recording_transport()
        action = {"type": "webhook", "url": "https://example.test/hook",
                  "payload": '{"order": "{trigger.data.total}"}'}
        run(action, EVENT, transport=transport)
        self.assertEqual(json.loads(transport.calls[0]["body"]), {"order": "42.00"})

    def test_payload_can_reach_earlier_steps(self):
        transport = recording_transport()
        action = {"type": "webhook", "url": "https://example.test/hook",
                  "payload": '{"key": "{steps.upload.output.key}"}'}
        steps = {"upload": {"status": "success", "output": {"key": "a/b.pdf"}}}
        run(action, EVENT, steps=steps, transport=transport)
        self.assertEqual(json.loads(transport.calls[0]["body"]), {"key": "a/b.pdf"})

    def test_signature_covers_the_shaped_body(self):
        transport = recording_transport()
        action = {"type": "webhook", "url": "https://example.test/hook",
                  "secret_id": "dapier/webhook",
                  "payload": '{"id": "{trigger.id}"}'}
        with patch("src.dapier.engine.actions.webhook.base._signing_secret",
                   return_value="s3cret"):
            run(action, EVENT, transport=transport)
        expected = hmac.new(b"s3cret", transport.calls[0]["body"], hashlib.sha256).hexdigest()
        self.assertEqual(transport.calls[0]["headers"]["x-dapier-signature"],
                         f"sha256={expected}")
        self.assertEqual(json.loads(transport.calls[0]["body"]), {"id": "evt-1"})

    def test_invalid_rendered_json_is_a_clear_error(self):
        action = {"type": "webhook", "url": "https://example.test/hook",
                  "payload": "not json at all"}
        with self.assertRaises(ValueError) as ctx:
            run(action, EVENT, transport=recording_transport())
        self.assertIn("webhook payload rendered to invalid JSON", str(ctx.exception))

    def test_registry_lambda_forwards_steps_to_the_runner(self):
        action = {"type": "webhook", "url": "https://example.test/hook",
                  "payload": '{"x": "{steps.a.output.y}"}'}
        steps = {"a": {"status": "success", "output": {"y": 7}}}
        with patch("src.dapier.connectors.webhook.run_webhook") as runner:
            registry.ACTIONS["webhook"].run(action, EVENT, "wf", steps=steps)
        self.assertEqual(runner.call_args.kwargs.get("steps"), steps)


class RegistryTests(unittest.TestCase):
    def test_registry_declares_the_payload_field(self):
        entry = registry.ACTIONS["webhook"]
        self.assertIn("payload", entry.optional)
        field = next(f for f in entry.fields if f["key"] == "payload")
        self.assertEqual(field["label"], "Payload (JSON, templated)")
        self.assertEqual(field["type"], "textarea")


if __name__ == "__main__":
    unittest.main()
