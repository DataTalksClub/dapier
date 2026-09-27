"""http_request auth variety, from the action spec's perspective: bearer,
basic and custom-header auth as flat templated fields, with the legacy
connection bearer as the no-auth-type default.

The transport is injected, so every test records the exact method/URL/headers
the runner produced — no network. Key names follow the connector's schema:
``auth_type`` of ``api_key`` is the custom-header auth (``auth_key_name`` /
``auth_key_value``); bearer/basic use ``auth_token`` / ``auth_username`` +
``auth_password``.
"""
import base64
import unittest
from unittest.mock import patch

from src.dapier.connectors import webhook as webhook_connector


def recording_transport(status=200, raw=b'{"ok": true}'):
    calls = []

    def transport(method, url, *, headers=None, body=None, timeout=15):
        calls.append({"method": method, "url": url, "headers": headers,
                      "body": body, "timeout": timeout})
        return status, raw

    transport.calls = calls
    return transport


def run(action, event=None, transport=None):
    return webhook_connector.run_http_request(action, event or {}, transport=transport)


class NoAuthTests(unittest.TestCase):
    def test_no_auth_fields_add_no_authorization_header(self):
        transport = recording_transport()
        run({"type": "http_request", "url": "https://example.test"}, transport=transport)
        self.assertEqual(transport.calls[0]["headers"], {})

    def test_explicit_none_also_sends_no_header(self):
        transport = recording_transport()
        run({"type": "http_request", "url": "https://example.test",
             "auth_type": "none"}, transport=transport)
        self.assertEqual(transport.calls[0]["headers"], {})


class BearerAuthTests(unittest.TestCase):
    def test_templated_token_becomes_the_bearer_header(self):
        transport = recording_transport()
        run({"type": "http_request", "url": "https://example.test", "auth_type": "bearer",
             "auth_token": "{trigger.token}"}, event={"data": {"token": "tok-7"}},
            transport=transport)
        self.assertEqual(transport.calls[0]["headers"]["authorization"], "Bearer tok-7")

    def test_explicit_token_wins_over_connection_id(self):
        transport = recording_transport()
        with patch.object(webhook_connector, "_connection_token",
                          return_value="conn-tok"):
            run({"type": "http_request", "url": "https://example.test",
                 "auth_type": "bearer", "auth_token": "explicit-tok",
                 "connection_id": "slack"}, transport=transport)
        self.assertEqual(transport.calls[0]["headers"]["authorization"], "Bearer explicit-tok")

    def test_bearer_without_a_token_is_a_clear_error(self):
        with self.assertRaises(ValueError):
            run({"type": "http_request", "url": "https://example.test",
                 "auth_type": "bearer"}, transport=recording_transport())


class BasicAuthTests(unittest.TestCase):
    def test_basic_header_decodes_to_user_password(self):
        transport = recording_transport()
        run({"type": "http_request", "url": "https://example.test", "auth_type": "basic",
             "auth_username": "ops", "auth_password": "s3cret"}, transport=transport)
        header = transport.calls[0]["headers"]["authorization"]
        self.assertTrue(header.startswith("Basic "))
        self.assertEqual(base64.b64decode(header[len("Basic "):]).decode(), "ops:s3cret")

    def test_basic_without_a_password_is_a_clear_error(self):
        with self.assertRaises(ValueError):
            run({"type": "http_request", "url": "https://example.test",
                 "auth_type": "basic", "auth_username": "ops"},
                transport=recording_transport())


class CustomHeaderAuthTests(unittest.TestCase):
    def test_api_key_lands_under_the_custom_header_name(self):
        transport = recording_transport()
        run({"type": "http_request", "url": "https://example.test", "auth_type": "api_key",
             "auth_key_name": "x-api-key", "auth_key_value": "{trigger.key}"},
            event={"data": {"key": "k-9"}}, transport=transport)
        headers = transport.calls[0]["headers"]
        self.assertEqual(headers["x-api-key"], "k-9")
        self.assertNotIn("authorization", headers)

    def test_header_auth_without_a_header_name_is_a_clear_error(self):
        with self.assertRaises(ValueError):
            run({"type": "http_request", "url": "https://example.test",
                 "auth_type": "api_key", "auth_key_value": "k"},
                transport=recording_transport())

    def test_unknown_auth_type_is_a_clear_error(self):
        with self.assertRaises(ValueError):
            run({"type": "http_request", "url": "https://example.test",
                 "auth_type": "hmac"}, transport=recording_transport())


class RegistryDeclarationTests(unittest.TestCase):
    def test_registry_declares_every_auth_field(self):
        from src.dapier.connectors import registry
        entry = registry.ACTIONS["http_request"]
        for key in ("auth_type", "auth_username", "auth_password", "auth_token",
                    "auth_key_name", "auth_key_value", "auth_key_in"):
            self.assertIn(key, entry.optional)
        auth_type = next(f for f in entry.fields if f["key"] == "auth_type")
        self.assertEqual(auth_type["type"], "select")
        self.assertEqual(auth_type["options"], list(webhook_connector.AUTH_TYPES))


if __name__ == "__main__":
    unittest.main()
