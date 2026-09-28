"""ai_complete action: registry dispatch, transport, json_mode, config seam.

The transport is injected (the webhook/http_request seam), so no test hits
the network; configuration is the copilot's env (COPILOT_LLM_*), set per
test. Content problems are verdicts ({ok: false}); transport problems raise
the engine's HttpError.
"""
import json
import os
import unittest
import urllib.error
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

from src.dapier.connectors import ai as ai_connector
from src.dapier.connectors import registry
from src.dapier.connectors.registry import ActionError, validate_action_chain
from src.dapier.engine import execute
from src.dapier.engine.actions.webhook import HttpError

ENV = {
    "COPILOT_LLM_API_KEY": "test-key",
    "COPILOT_LLM_BASE_URL": "https://llm.example.test/v1",
    "COPILOT_LLM_MODEL": "test-model",
}

CATALOG_TS = Path(__file__).resolve().parents[1] / "designer" / "src" / "catalog.ts"


@contextmanager
def configured(**overrides):
    """The copilot env, as the Worker would carry it (docs/connectors/ai.md)."""
    with patch.dict(os.environ, {**ENV, **overrides}):
        yield


@contextmanager
def unconfigured():
    """No API key anywhere in the environment."""
    with patch.dict(os.environ):
        os.environ.pop("COPILOT_LLM_API_KEY", None)
        yield


def llm_reply(content, model="test-model", usage=None):
    """One OpenAI-compatible /chat/completions response body."""
    return json.dumps({
        "model": model,
        "choices": [{"message": {"role": "assistant", "content": content}}],
        "usage": usage if usage is not None
        else {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5},
    }).encode()


def stub_transport(status=200, raw=None):
    """A transport that records the request and answers once, like the
    http_request tests' recording_transport."""
    calls = []

    def transport(method, url, *, headers=None, body=None, timeout=25):
        calls.append({"method": method, "url": url, "headers": headers,
                      "payload": json.loads(body.decode()), "timeout": timeout})
        return status, llm_reply("ok") if raw is None else raw

    transport.calls = calls
    return transport


def run(action, event=None, *, steps=None, transport=None):
    return ai_connector.run_ai_complete(action, event or {}, steps=steps,
                                        transport=transport)


class DispatchTests(unittest.TestCase):
    """The registry dispatches ai_complete like any connector action."""

    def test_run_action_reaches_the_runner(self):
        # run_action passes no transport, so stub the default transport the
        # registry dispatch actually lands on.
        with configured(), patch.object(ai_connector, "_default_transport",
                                        return_value=(200, llm_reply("hi there"))):
            output = registry.run_action(
                {"type": "ai_complete", "prompt": "say hi"}, {"data": {}})
        assert output["ok"] is True
        assert output["text"] == "hi there"

    def test_validate_action_chain_enforces_the_keys(self):
        with configured():
            validate_action_chain([{"type": "ai_complete", "prompt": "hi"}])
            try:
                validate_action_chain([{"type": "ai_complete"}])
            except ActionError as exc:
                assert "missing: prompt" in str(exc)
            else:
                raise AssertionError("missing prompt must fail the save")
            try:
                validate_action_chain(
                    [{"type": "ai_complete", "prompt": "hi", "bogus": 1}])
            except ActionError as exc:
                assert "unknown keys: bogus" in str(exc)
            else:
                raise AssertionError("unknown keys must fail the save")


class TextCompletionTests(unittest.TestCase):
    def test_happy_path_returns_text_model_usage(self):
        transport = stub_transport(raw=llm_reply("hello there"))
        with configured():
            output = run({"type": "ai_complete", "prompt": "greet"}, transport=transport)
        assert output == {
            "ok": True, "text": "hello there", "model": "test-model",
            "usage": {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5},
        }
        call = transport.calls[0]
        assert call["url"] == "https://llm.example.test/v1/chat/completions"
        assert call["headers"]["authorization"] == "Bearer test-key"
        assert call["payload"]["model"] == "test-model"
        assert call["payload"]["messages"] == [{"role": "user", "content": "greet"}]
        assert call["payload"]["temperature"] == 0
        assert "response_format" not in call["payload"]

    def test_prompt_and_system_render_from_the_event(self):
        transport = stub_transport()
        with configured():
            run({"type": "ai_complete", "prompt": "Summarize: {body}",
                 "system": "Be brief, {who}"}, {"data": {"body": "a long memo", "who": "ops"}},
                transport=transport)
        assert transport.calls[0]["payload"]["messages"] == [
            {"role": "system", "content": "Be brief, ops"},
            {"role": "user", "content": "Summarize: a long memo"},
        ]

    def test_steps_context_renders_into_the_prompt(self):
        transport = stub_transport()
        steps = {"find": {"status": "completed", "output": {"draft": "Q3 numbers"}}}
        with configured():
            run({"type": "ai_complete", "prompt": "Polish: {steps.find.output.draft}"},
                steps=steps, transport=transport)
        assert transport.calls[0]["payload"]["messages"][0]["content"] == \
            "Polish: Q3 numbers"

    def test_empty_rendered_prompt_is_a_clear_error(self):
        transport = stub_transport()
        with configured():
            try:
                run({"type": "ai_complete", "prompt": "{missing}"}, {"data": {}},
                    transport=transport)
            except ValueError as exc:
                assert "requires a rendered prompt" in str(exc)
            else:
                raise AssertionError("an empty prompt must fail the step")
        assert transport.calls == []


class JsonModeTests(unittest.TestCase):
    def test_json_mode_requests_and_parses_the_object(self):
        transport = stub_transport(raw=llm_reply('{"summary": "ok", "score": 4}'))
        with configured():
            output = run({"type": "ai_complete", "prompt": "judge",
                          "json_mode": True}, transport=transport)
        assert output["ok"] is True
        assert output["data"] == {"summary": "ok", "score": 4}
        assert "text" not in output
        assert transport.calls[0]["payload"]["response_format"] == {"type": "json_object"}

    def test_json_mode_parses_a_fenced_reply(self):
        transport = stub_transport(raw=llm_reply('```json\n{"a": 1}\n```'))
        with configured():
            output = run({"type": "ai_complete", "prompt": "x",
                          "json_mode": "true"}, transport=transport)
        assert output["ok"] is True
        assert output["data"] == {"a": 1}

    def test_json_mode_parse_failure_is_a_verdict_not_a_raise(self):
        transport = stub_transport(raw=llm_reply("no JSON here, sorry"))
        with configured():
            output = run({"type": "ai_complete", "prompt": "x",
                          "json_mode": True}, transport=transport)
        assert output["ok"] is False
        assert "JSON" in output["error"]
        assert output["text"] == "no JSON here, sorry"
        assert output["model"] == "test-model"


class ConfigTests(unittest.TestCase):
    def test_unconfigured_env_fails_with_the_setup_message(self):
        with unconfigured():
            try:
                run({"type": "ai_complete", "prompt": "x"},
                    transport=stub_transport())
            except ValueError as exc:
                message = str(exc)
                assert "ai_complete is not configured" in message
                assert "COPILOT_LLM_API_KEY" in message
                assert "COPILOT_LLM_BASE_URL" in message
                assert "COPILOT_LLM_MODEL" in message
            else:
                raise AssertionError("unconfigured env must refuse the step")

    def test_model_and_temperature_pass_through(self):
        transport = stub_transport()
        with configured():
            run({"type": "ai_complete", "prompt": "x", "model": "gpt-x",
                 "temperature": 0.7}, transport=transport)
        payload = transport.calls[0]["payload"]
        assert payload["model"] == "gpt-x"
        assert payload["temperature"] == 0.7

    def test_temperature_out_of_range_or_non_numeric_refuses(self):
        transport = stub_transport()
        with configured():
            for value in (2.5, -0.1, "warm"):
                try:
                    run({"type": "ai_complete", "prompt": "x", "temperature": value},
                        transport=transport)
                except ValueError as exc:
                    assert "temperature" in str(exc)
                else:
                    raise AssertionError(f"temperature {value!r} must refuse")
        assert transport.calls == []


class EngineErrorTests(unittest.TestCase):
    """Transport problems arrive as the engine's HttpError, Retry-After and
    all — the type autoretry's 429/503 pacing reads."""

    def test_http_4xx_from_the_default_transport_is_typed(self):
        error = urllib.error.HTTPError(
            "https://llm.example.test/v1/chat/completions", 429, "Slow down",
            {"retry-after": "7"}, None)
        with configured(), patch.object(ai_connector, "_default_transport",
                                        side_effect=error):
            try:
                run({"type": "ai_complete", "prompt": "x"})
            except HttpError as exc:
                assert exc.status == 429
                assert exc.retry_after == 7.0
            else:
                raise AssertionError("HTTP 429 must raise HttpError")

    def test_retry_after_hint_reaches_the_engine_backoff(self):
        from src.dapier.engine import logic

        error = HttpError("ai_complete returned HTTP 503", status=503, retry_after=7.0)
        assert logic._retry_after_hint(error, {"max_seconds": 60}) == 7.0

    def test_http_5xx_from_the_injected_transport_is_typed(self):
        transport = stub_transport(status=503, raw=b"upstream exploded")
        with configured():
            try:
                run({"type": "ai_complete", "prompt": "x"}, transport=transport)
            except HttpError as exc:
                assert exc.status == 503
            else:
                raise AssertionError("HTTP 503 must raise HttpError")

    def test_unreachable_endpoint_is_typed(self):
        with configured(), patch.object(
                ai_connector, "_default_transport",
                side_effect=urllib.error.URLError("connection refused")):
            try:
                run({"type": "ai_complete", "prompt": "x"})
            except HttpError as exc:
                assert "connection refused" in str(exc)
                assert exc.status is None
            else:
                raise AssertionError("an unreachable endpoint must raise HttpError")

    def test_unexpected_response_shape_is_typed(self):
        transport = stub_transport(raw=b'{"nope": true}')
        with configured():
            try:
                run({"type": "ai_complete", "prompt": "x"}, transport=transport)
            except HttpError as exc:
                assert "unexpected response" in str(exc)
            else:
                raise AssertionError("a malformed body must raise HttpError")

    def test_engine_execute_records_the_failure(self):
        workflow = {
            "id": "wf-ai", "enabled": True,
            "trigger": {"connector": "email", "event": "message.received", "filters": {}},
            "actions": [{"id": "ai-step", "type": "ai_complete", "prompt": "hi"}],
        }
        errors = []
        hooks = {
            "before_action": lambda *args: True,
            "on_action_error": lambda *args, **kw: errors.append(str(args[3])),
        }
        with configured(), patch("src.dapier.engine.all_workflows",
                                 return_value=[workflow]), \
             patch.object(ai_connector, "_default_transport",
                          side_effect=urllib.error.URLError("down")), \
             self.assertRaises(HttpError):
            execute({"id": "evt-ai", "connector": "email",
                     "event": "message.received", "data": {}}, **hooks)
        assert errors and "down" in errors[0]


class ChipMirrorTests(unittest.TestCase):
    """The palette chip and its designer mirror stay pinned, like the
    designer pickers pin the action catalog."""

    def test_chip_is_registered_without_a_connection(self):
        chip = registry.CONNECTORS.get("ai")
        assert chip is not None, "the ai chip must be registered by connectors.ai"
        assert chip.events == ()
        assert chip.icon == "sparkles"

    def test_catalog_json_carries_the_chip_and_action(self):
        catalog = registry.catalog()
        assert {"name": "ai", "label": "AI", "events": [], "icon": "sparkles"} \
            in catalog["connectors"]
        assert any(entry["type"] == "ai_complete" for entry in catalog["actions"])

    def test_chip_and_action_reach_the_designer_mirror(self):
        content = CATALOG_TS.read_text()
        assert '{ name: "ai", label: "AI", logo: Sparkles, events: [] }' in content
        start = content.index('type: "ai_complete"')
        end = content.find('\n    type: "', start + 1)
        block = content[start:end]
        for field in registry.ACTIONS["ai_complete"].fields:
            assert f'key: "{field["key"]}"' in block, (
                f"designer ai_complete is missing the {field['key']} field")


if __name__ == "__main__":
    import unittest

    unittest.main()
