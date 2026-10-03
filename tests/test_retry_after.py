"""Autoretry honors HTTP 429/503 responses and their ``Retry-After`` hint.

The webhook and http_request actions raise a typed ``HttpError`` (still a
RuntimeError) carrying the response ``status`` and, when the transport
surfaced headers, the server's ``retry_after`` seconds. A step opted into
``autoretry`` waits that hinted delay instead of its exponential plan —
capped at the plan's ``max_seconds`` — and any failure without a usable
hint backs off exactly as before. Without the opt-in nothing retries: the
typed error propagates on the first attempt, unchanged.
"""

import unittest
from unittest.mock import patch

import pytest

from src.dapier.engine import logic
from src.dapier.engine.actions import webhook as webhook_action
from src.dapier.engine.actions.webhook import HttpError

EVENT = {
    "id": "evt-1",
    "connector": "email",
    "event": "message.received",
    "data": {"route": "invoice"},
}


def run_chain(steps, *, run_action):
    stop = logic.run_chain(
        "wf-1", steps, EVENT, run_action,
        before_action=lambda *a, **k: True,
        after_action=lambda *a, **k: None,
        on_action_error=lambda *a, **k: None,
    )
    return stop


def sequence_runner(errors):
    """Fail with each error in turn, then succeed. Returns (runner, calls)."""
    calls = []

    def runner(action, event, workflow_id, steps=None):
        calls.append(action.get("id"))
        if len(calls) <= len(errors):
            raise errors[len(calls) - 1]
        return {"ok": True}

    return runner, calls


def stepped_sleep():
    """Patch jitter to zero and record every backoff sleep."""
    sleeps = []
    uniform = patch("src.dapier.engine.logic_pkg.execution.random.uniform", return_value=0.0)
    sleeper = patch("src.dapier.engine.logic_pkg.execution.time.sleep",
                    side_effect=lambda seconds: sleeps.append(seconds))
    return sleeps, uniform, sleeper


class TypedErrorTests(unittest.TestCase):
    """The webhook/http actions attach status (and Retry-After) info."""

    def test_webhook_transport_429_raises_the_typed_error(self):
        with pytest.raises(HttpError) as exc:
            webhook_action.run_webhook(
                {"type": "webhook", "url": "https://example.test/hook"}, {},
                transport=lambda *a, **k: (429, b"slow down"))
        assert exc.value.status == 429
        assert exc.value.retry_after is None  # the injected transport hides headers

    def test_webhook_urlopen_429_keeps_the_retry_after_header(self):
        import urllib.error

        error = urllib.error.HTTPError(
            "https://example.test/hook", 429, "Too Many Requests",
            {"retry-after": "17"}, None)

        def fake_urlopen(request, timeout=None):
            raise error

        with patch("src.dapier.engine.actions.webhook.urllib.request.urlopen",
                   side_effect=fake_urlopen):
            with pytest.raises(HttpError) as exc:
                webhook_action.run_webhook(
                    {"type": "webhook", "url": "https://example.test/hook"}, {})
        assert exc.value.status == 429
        assert exc.value.retry_after == 17.0

    def test_http_request_transport_503_raises_the_typed_error(self):
        from src.dapier.connectors.webhook import HttpError as ConnectorHttpError

        with pytest.raises(ConnectorHttpError) as exc:
            from src.dapier.connectors.webhook import run_http_request
            run_http_request(
                {"type": "http_request", "url": "https://api.example.test/items"}, {},
                transport=lambda *a, **k: (503, b"unavailable"))
        assert exc.value.status == 503

    def test_typed_error_is_still_a_runtime_error(self):
        # Existing handlers (on_fail, run history, tests) catch RuntimeError.
        assert issubclass(HttpError, RuntimeError)


class RetryAfterBackoffTests(unittest.TestCase):
    """The hinted delay replaces the doubling backoff, capped at max_seconds."""

    STEP = {"id": "post", "type": "webhook", "url": "https://example.test"}

    def test_429_retry_after_is_honored(self):
        runner, calls = sequence_runner([
            HttpError("rate limited", status=429, retry_after=30.0),
            HttpError("rate limited", status=429, retry_after=30.0),
        ])
        steps = [{**self.STEP, "autoretry": {"attempts": 2}}]
        sleeps, uniform, sleeper = stepped_sleep()
        with uniform, sleeper:
            stop = run_chain(steps, run_action=runner)

        assert stop is None
        assert calls == ["post", "post", "post"]
        # The server asked for 30s; both 429s get it instead of 1s/2s.
        assert sleeps == [30.0, 30.0]

    def test_503_is_treated_like_a_rate_limit(self):
        runner, calls = sequence_runner([
            HttpError("unavailable", status=503, retry_after=8.0),
        ])
        steps = [{**self.STEP, "autoretry": {"attempts": 1}}]
        sleeps, uniform, sleeper = stepped_sleep()
        with uniform, sleeper:
            stop = run_chain(steps, run_action=runner)

        assert stop is None
        assert sleeps == [8.0]

    def test_hint_is_capped_at_the_plan_max(self):
        runner, _calls = sequence_runner([
            HttpError("rate limited", status=429, retry_after=500.0),
        ])
        steps = [{**self.STEP, "autoretry": {"attempts": 1, "max_seconds": 5}}]
        sleeps, uniform, sleeper = stepped_sleep()
        with uniform, sleeper:
            run_chain(steps, run_action=runner)

        assert sleeps == [5.0]

    def test_other_failures_keep_the_exponential_backoff(self):
        runner, _calls = sequence_runner([
            HttpError("rate limited", status=429, retry_after=30.0),
            RuntimeError("plain crash"),
        ])
        steps = [{**self.STEP, "autoretry": {"attempts": 2}}]
        sleeps, uniform, sleeper = stepped_sleep()
        with uniform, sleeper:
            run_chain(steps, run_action=runner)

        # The hinted first wait, then the plan's normal doubling (1s -> 2s).
        assert sleeps == [30.0, 2.0]

    def test_429_without_a_usable_hint_backs_off_normally(self):
        for error in (HttpError("rate limited", status=429),
                      HttpError("rate limited", status=429, retry_after=0),
                      HttpError("rate limited", status=429, retry_after="soon"),
                      HttpError("boom", status=500, retry_after=99.0)):
            with self.subTest(error=repr(error)):
                runner, _calls = sequence_runner([error])
                steps = [{**self.STEP, "autoretry": {"attempts": 1}}]
                sleeps, uniform, sleeper = stepped_sleep()
                with uniform, sleeper:
                    run_chain(steps, run_action=runner)
                assert sleeps == [1.0]  # the plan's initial delay

    def test_non_autoretry_steps_still_fail_on_the_first_attempt(self):
        runner, calls = sequence_runner([
            HttpError("rate limited", status=429, retry_after=30.0),
        ])
        sleeps, uniform, sleeper = stepped_sleep()
        with uniform, sleeper:
            with pytest.raises(HttpError):
                run_chain([{"id": "post", "type": "webhook",
                            "url": "https://example.test"}], run_action=runner)

        assert calls == ["post"]  # no retry without the opt-in
        assert sleeps == []  # and no sleep either


if __name__ == "__main__":
    pytest.main([__file__])
