"""Response headers on the webhook/http_request step output.

An upgraded transport (or the urllib fallback) surfaces the response's
headers, and they land lowercased in ``response_headers`` beside
``status``/``body``. A legacy ``(status, raw)`` transport keeps the exact
two-key output — the key only appears when there is something in it.
"""
import json
import unittest
from contextlib import contextmanager
from unittest.mock import patch

from src.dapier.connectors import webhook as webhook_connector
from src.dapier.engine.actions import webhook as webhook_action
from src.dapier.engine.actions.webhook import HttpError


class FakeResponse:
    def __init__(self, status=200, body=b"", headers=None):
        self.status = status
        self._body = body
        self.headers = headers or {}

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False


@contextmanager
def urlopen_returns(response, module):
    target = f"{module}.urllib.request.urlopen"

    def fake_urlopen(request, timeout=None):
        return response

    with patch(target, side_effect=fake_urlopen):
        yield


def upgraded_transport(status=200, raw=b'{"ok": true}', response_headers=None):
    """A transport that surfaces headers: (status, raw, headers)."""
    return lambda method, url, *, headers=None, body=None, timeout=15: (
        status, raw, response_headers or {})


def legacy_transport(status=200, raw=b'{"ok": true}'):
    """Today's transport shape: (status, raw), no headers."""
    return lambda method, url, *, headers=None, body=None, timeout=15: (status, raw)


ACTION = {"type": "http_request", "url": "https://example.test/items"}


class HttpResponseHeaderTests(unittest.TestCase):
    def test_headers_land_lowercased_beside_status_and_body(self):
        transport = upgraded_transport(response_headers={"Content-Type": "application/json",
                                                         "X-Request-Id": "r-1"})
        output = webhook_connector.run_http_request(dict(ACTION), {}, transport=transport)
        self.assertEqual(output["status"], 200)
        self.assertEqual(output["body"], {"ok": True})
        self.assertEqual(output["response_headers"],
                         {"content-type": "application/json", "x-request-id": "r-1"})

    def test_legacy_transport_keeps_the_two_key_output(self):
        transport = legacy_transport(raw=b"plain text")
        output = webhook_connector.run_http_request(dict(ACTION), {}, transport=transport)
        self.assertEqual(output, {"status": 200, "body": "plain text"})

    def test_header_values_are_json_safe_strings(self):
        transport = upgraded_transport(response_headers={"X-Retry-In": 3, "X-Flag": True})
        output = webhook_connector.run_http_request(dict(ACTION), {}, transport=transport)
        self.assertEqual(output["response_headers"], {"x-retry-in": "3", "x-flag": "True"})
        json.loads(json.dumps(output))

    def test_error_status_still_raises_typed(self):
        transport = upgraded_transport(status=500, raw=b"boom")
        with self.assertRaises(HttpError) as caught:
            webhook_connector.run_http_request(dict(ACTION), {}, transport=transport)
        self.assertEqual(caught.exception.status, 500)

    def test_urllib_fallback_surfaces_the_real_response_headers(self):
        response = FakeResponse(body=b'{"ok": true}',
                                headers={"Content-Type": "application/json",
                                         "X-Trace": "t-9"})
        with urlopen_returns(response, "src.dapier.connectors.webhook"):
            output = webhook_connector.run_http_request(dict(ACTION), {})
        self.assertEqual(output["response_headers"],
                         {"content-type": "application/json", "x-trace": "t-9"})

    def test_urllib_fallback_still_sends_the_same_request(self):
        seen = {}

        def fake_urlopen(request, timeout=None):
            seen["url"] = request.full_url
            seen["method"] = request.get_method()
            seen["timeout"] = timeout
            return FakeResponse(body=b"ok")

        with patch("src.dapier.connectors.webhook.urllib.request.urlopen",
                   side_effect=fake_urlopen):
            webhook_connector.run_http_request(
                {**ACTION, "method": "PUT", "headers": {"x-trace": "{id}"},
                 "timeout_seconds": 4}, {"id": "e-1"})
        self.assertEqual(seen["url"], "https://example.test/items")
        self.assertEqual(seen["method"], "PUT")
        self.assertEqual(seen["timeout"], 4)


class WebhookResponseHeaderTests(unittest.TestCase):
    ACTION = {"type": "webhook", "url": "https://example.test/hook"}

    def test_urllib_path_surfaces_response_headers(self):
        response = FakeResponse(body=b'{"answer": 42}', headers={"X-Rec": "yes"})
        with urlopen_returns(response, "src.dapier.engine.actions.webhook"):
            output = webhook_action.run_webhook(dict(self.ACTION), {"hello": "world"})
        self.assertEqual(output, {"status": 200, "body": {"answer": 42},
                                  "response_headers": {"x-rec": "yes"}})

    def test_upgraded_transport_surfaces_response_headers(self):
        transport = upgraded_transport(response_headers={"X-Rec": "yes"})
        output = webhook_action.run_webhook(dict(self.ACTION), {}, transport=transport)
        self.assertEqual(output["response_headers"], {"x-rec": "yes"})

    def test_legacy_transport_keeps_the_two_key_output(self):
        output = webhook_action.run_webhook(dict(self.ACTION), {}, transport=legacy_transport())
        self.assertEqual(output, {"status": 200, "body": {"ok": True}})


if __name__ == "__main__":
    import pytest

    pytest.main([__file__])
