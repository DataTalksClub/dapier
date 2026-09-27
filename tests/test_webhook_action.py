"""The webhook action's response body: additive step output, like http_request.

The runner used to return only ``{"status": ...}``; now the body lands in the
output (parsed JSON, else a text preview) so signed webhooks can chain on the
receiver's answer.
"""
import hashlib
import hmac
import unittest
from contextlib import contextmanager
from unittest.mock import patch

from src.dapier.engine.actions import webhook as webhook_action


class FakeResponse:
    def __init__(self, status=200, body=b""):
        self.status = status
        self._body = body

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False


@contextmanager
def urlopen_returns(response, record=None):
    def fake_urlopen(request, timeout=None):
        if record is not None:
            record["request"] = request
            record["timeout"] = timeout
        return response

    with patch("src.dapier.engine.actions.webhook.urllib.request.urlopen",
               side_effect=fake_urlopen):
        yield


ACTION = {"type": "webhook", "url": "https://example.test/hook"}


class WebhookBodyTests(unittest.TestCase):
    def test_json_body_is_parsed_into_the_output(self):
        with urlopen_returns(FakeResponse(body=b'{"answer": 42}')):
            output = webhook_action.run_webhook(dict(ACTION), {"hello": "world"})
        self.assertEqual(output, {"status": 200, "body": {"answer": 42}})

    def test_non_json_body_comes_back_as_text(self):
        with urlopen_returns(FakeResponse(body=b"accepted, thanks")):
            output = webhook_action.run_webhook(dict(ACTION), {})
        self.assertEqual(output, {"status": 200, "body": "accepted, thanks"})

    def test_oversized_text_body_is_capped_like_http_request(self):
        with urlopen_returns(FakeResponse(body=b"x" * 9000)):
            output = webhook_action.run_webhook(dict(ACTION), {})
        self.assertEqual(output["body"], "x" * 4000)

    def test_error_status_still_raises(self):
        with urlopen_returns(FakeResponse(status=500, body=b"boom")), \
                self.assertRaises(RuntimeError):
            webhook_action.run_webhook(dict(ACTION), {})

    def test_signature_and_request_body_are_unchanged(self):
        seen = {}
        with urlopen_returns(FakeResponse(body=b'{"ok": true}'), record=seen), \
                patch("src.dapier.engine.actions.webhook.base._signing_secret",
                      return_value="s3cret"):
            output = webhook_action.run_webhook({**ACTION, "secret_id": "dapier/webhook"}, {})

        request = seen["request"]
        expected = hmac.new(b"s3cret", request.data, hashlib.sha256).hexdigest()
        self.assertEqual(request.get_header("X-dapier-signature"), f"sha256={expected}")
        self.assertEqual(output, {"status": 200, "body": {"ok": True}})


if __name__ == "__main__":
    import pytest

    pytest.main([__file__])
