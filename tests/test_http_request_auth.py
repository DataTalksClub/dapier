"""http_request auth variety: basic, bearer (direct or via connection) and
API-key auth, beside the legacy bearer-from-connection default.

The transport is injected, so every test records the exact method/URL/headers
the runner produced — no network.
"""
import base64
import unittest
from unittest.mock import patch

from src.dapier.connectors import registry
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


class BasicAuthTests(unittest.TestCase):
    def test_username_and_password_become_a_basic_header(self):
        transport = recording_transport()
        run({"type": "http_request", "url": "https://example.test", "auth_type": "basic",
             "auth_username": "ops", "auth_password": "s3cret"}, transport=transport)
        expected = base64.b64encode(b"ops:s3cret").decode()
        self.assertEqual(transport.calls[0]["headers"]["authorization"], f"Basic {expected}")

    def test_credentials_are_templated(self):
        transport = recording_transport()
        run({"type": "http_request", "url": "https://example.test", "auth_type": "basic",
             "auth_username": "{trigger.user}", "auth_password": "{trigger.pass}"},
            event={"data": {"user": "amy", "pass": "pw"}}, transport=transport)
        expected = base64.b64encode(b"amy:pw").decode()
        self.assertEqual(transport.calls[0]["headers"]["authorization"], f"Basic {expected}")

    def test_missing_password_is_a_clear_error(self):
        with self.assertRaises(ValueError, msg="basic auth needs auth_username and auth_password"):
            run({"type": "http_request", "url": "https://example.test",
                 "auth_type": "basic", "auth_username": "ops"}, transport=recording_transport())


class BearerAuthTests(unittest.TestCase):
    def test_explicit_token_is_sent(self):
        transport = recording_transport()
        run({"type": "http_request", "url": "https://example.test", "auth_type": "bearer",
             "auth_token": "{trigger.tok}"}, event={"data": {"tok": "tok-1"}},
            transport=transport)
        self.assertEqual(transport.calls[0]["headers"]["authorization"], "Bearer tok-1")

    def test_explicit_token_wins_over_connection(self):
        transport = recording_transport()
        with patch.object(webhook_connector, "_connection_token",
                                        return_value="conn-tok"):
            run({"type": "http_request", "url": "https://example.test",
                 "auth_type": "bearer", "auth_token": "direct",
                 "connection_id": "slack"}, transport=transport)
        self.assertEqual(transport.calls[0]["headers"]["authorization"], "Bearer direct")

    def test_connection_id_is_the_token_fallback(self):
        transport = recording_transport()
        with patch.object(webhook_connector, "_connection_token",
                                        return_value="conn-tok"):
            run({"type": "http_request", "url": "https://example.test",
                 "auth_type": "bearer", "connection_id": "slack"}, transport=transport)
        self.assertEqual(transport.calls[0]["headers"]["authorization"], "Bearer conn-tok")

    def test_bearer_without_any_token_is_a_clear_error(self):
        with self.assertRaises(ValueError, msg="bearer auth needs auth_token or connection_id"):
            run({"type": "http_request", "url": "https://example.test",
                 "auth_type": "bearer"}, transport=recording_transport())

    def test_no_auth_type_keeps_the_connection_bearer_default(self):
        transport = recording_transport()
        with patch.object(webhook_connector, "_connection_token",
                                        return_value="legacy"):
            run({"type": "http_request", "url": "https://example.test",
                 "connection_id": "slack"}, transport=transport)
        self.assertEqual(transport.calls[0]["headers"]["authorization"], "Bearer legacy")


class ApiKeyAuthTests(unittest.TestCase):
    def test_key_lands_in_a_header_by_default(self):
        transport = recording_transport()
        run({"type": "http_request", "url": "https://example.test", "auth_type": "api_key",
             "auth_key_name": "x-api-key", "auth_key_value": "k-123"}, transport=transport)
        headers = transport.calls[0]["headers"]
        self.assertEqual(headers["x-api-key"], "k-123")
        self.assertNotIn("authorization", headers)

    def test_key_can_ride_in_the_query(self):
        transport = recording_transport()
        run({"type": "http_request", "url": "https://example.test/v1", "auth_type": "api_key",
             "auth_key_name": "api-key", "auth_key_value": "k-123",
             "auth_key_in": "query"}, transport=transport)
        self.assertEqual(transport.calls[0]["url"], "https://example.test/v1?api-key=k-123")

    def test_query_key_appends_to_an_existing_query(self):
        transport = recording_transport()
        run({"type": "http_request", "url": "https://example.test?v=1",
             "auth_type": "api_key", "auth_key_name": "api-key",
             "auth_key_value": "k-123", "auth_key_in": "query"}, transport=transport)
        self.assertEqual(transport.calls[0]["url"], "https://example.test?v=1&api-key=k-123")

    def test_api_key_ignores_the_connection(self):
        transport = recording_transport()
        with patch.object(webhook_connector, "_connection_token",
                                        return_value="conn-tok"):
            run({"type": "http_request", "url": "https://example.test",
                 "auth_type": "api_key", "auth_key_name": "x-key",
                 "auth_key_value": "k", "connection_id": "slack"}, transport=transport)
        self.assertNotIn("authorization", transport.calls[0]["headers"])

    def test_api_key_overrides_a_same_named_custom_header(self):
        transport = recording_transport()
        run({"type": "http_request", "url": "https://example.test", "auth_type": "api_key",
             "auth_key_name": "x-trace", "auth_key_value": "t-1",
             "headers": {"x-trace": "static"}}, transport=transport)
        self.assertEqual(transport.calls[0]["headers"]["x-trace"], "t-1")

    def test_missing_key_name_is_a_clear_error(self):
        with self.assertRaises(ValueError, msg="api_key auth needs auth_key_name and auth_key_value"):
            run({"type": "http_request", "url": "https://example.test",
                 "auth_type": "api_key", "auth_key_value": "k"}, transport=recording_transport())

    def test_unknown_placement_is_a_clear_error(self):
        with self.assertRaises(ValueError, msg="auth_key_in must be header or query"):
            run({"type": "http_request", "url": "https://example.test",
                 "auth_type": "api_key", "auth_key_name": "k", "auth_key_value": "v",
                 "auth_key_in": "cookie"}, transport=recording_transport())


class AuthTypeValidationTests(unittest.TestCase):
    def test_unknown_auth_type_is_a_clear_error(self):
        with self.assertRaises(ValueError, msg="auth_type must be one of"):
            run({"type": "http_request", "url": "https://example.test",
                 "auth_type": "digest"}, transport=recording_transport())

    def test_registry_declares_the_auth_fields(self):
        entry = registry.ACTIONS["http_request"]
        for key in ("auth_type", "auth_username", "auth_password", "auth_token",
                    "auth_key_name", "auth_key_value", "auth_key_in"):
            self.assertIn(key, entry.optional)
        auth_type = next(f for f in entry.fields if f["key"] == "auth_type")
        self.assertEqual(auth_type["options"], list(webhook_connector.AUTH_TYPES))


if __name__ == "__main__":
    unittest.main()
