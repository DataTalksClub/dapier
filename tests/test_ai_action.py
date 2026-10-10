"""ai_complete action: registry dispatch, transport, json_mode, config seam.

The transport is injected (the webhook/http_request seam), so no test hits
the network; configuration is the LLM_* env, set per
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
    "LLM_API_KEY": "test-key",
    "LLM_BASE_URL": "https://llm.example.test/v1",
    "LLM_MODEL": "test-model",
}

CATALOG_TS = Path(__file__).resolve().parents[1] / "designer" / "src" / "catalog.ts"


@contextmanager
def configured(**overrides):
    """The LLM env, as the Worker would carry it (docs/connectors/ai.md)."""
    with patch.dict(os.environ, {**ENV, **overrides}):
        yield


@contextmanager
def unconfigured():
    """No API key anywhere in the environment."""
    with patch.dict(os.environ):
        os.environ.pop("LLM_API_KEY", None)
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
        # Temperature is omitted unless the step sets it: newer Claude
        # models reject the field outright, and every provider has its own
        # default.
        assert "temperature" not in call["payload"]
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


class JsonSchemaTests(unittest.TestCase):
    """Generic provider-agnostic structured extraction via `json_schema`."""

    SCHEMA = {
        "type": "object",
        "required": ["newsletter", "sponsors"],
        "properties": {
            "newsletter": {"type": "string"},
            "sponsors": {
                "type": "array",
                "items": {
                    "type": "object",
                    "required": ["name"],
                    "properties": {
                        "name": {"type": "string"},
                        "url": {"type": "string"},
                    },
                },
            },
        },
    }

    GOOD_REPLY = '{"newsletter": "Weekly", "sponsors": [{"name": "Acme"}]}'

    def test_json_schema_sends_structured_format_and_parses(self):
        transport = stub_transport(raw=llm_reply(self.GOOD_REPLY))
        with configured():
            output = run({"type": "ai_complete", "prompt": "extract",
                          "json_schema": self.SCHEMA}, transport=transport)
        assert output["ok"] is True
        assert output["data"] == {"newsletter": "Weekly", "sponsors": [{"name": "Acme"}]}
        assert "text" not in output
        assert transport.calls[0]["payload"]["response_format"] == {
            "type": "json_schema",
            "json_schema": {"name": "extraction", "schema": self.SCHEMA},
        }

    def test_json_schema_implies_json_mode(self):
        # No json_mode key at all — the schema alone requests structured output.
        transport = stub_transport(raw=llm_reply(self.GOOD_REPLY))
        with configured():
            output = run({"type": "ai_complete", "prompt": "extract",
                          "json_schema": self.SCHEMA,
                          "model": "anthropic/claude-haiku-4-5"},
                         transport=transport)
        assert output["ok"] is True
        payload = transport.calls[0]["payload"]
        assert payload["model"] == "anthropic/claude-haiku-4-5"
        assert payload["response_format"]["type"] == "json_schema"

    def test_json_schema_accepts_a_json_string(self):
        transport = stub_transport(raw=llm_reply(self.GOOD_REPLY))
        with configured():
            output = run({"type": "ai_complete", "prompt": "extract",
                          "json_schema": json.dumps(self.SCHEMA)},
                         transport=transport)
        assert output["ok"] is True
        assert output["data"]["newsletter"] == "Weekly"

    def test_json_schema_missing_required_field_is_a_verdict(self):
        transport = stub_transport(
            raw=llm_reply('{"newsletter": "Weekly", "sponsors": []}'))
        schema = {"type": "object", "required": ["newsletter", "sponsors", "edition"],
                  "properties": {"newsletter": {"type": "string"}}}
        with configured():
            output = run({"type": "ai_complete", "prompt": "x",
                          "json_schema": schema}, transport=transport)
        assert output["ok"] is False
        assert "edition" in output["error"]
        assert output["text"] == '{"newsletter": "Weekly", "sponsors": []}'
        assert output["model"] == "test-model"
        assert "usage" in output

    def test_json_schema_type_mismatch_is_a_verdict(self):
        transport = stub_transport(
            raw=llm_reply('{"newsletter": 42, "sponsors": []}'))
        with configured():
            output = run({"type": "ai_complete", "prompt": "x",
                          "json_schema": self.SCHEMA}, transport=transport)
        assert output["ok"] is False
        assert "newsletter" in output["error"]
        assert "string" in output["error"]

    def test_json_schema_array_item_mismatch_is_a_verdict(self):
        transport = stub_transport(raw=llm_reply(
            '{"newsletter": "W", "sponsors": [{"url": "https://x.test"}]}'))
        with configured():
            output = run({"type": "ai_complete", "prompt": "x",
                          "json_schema": self.SCHEMA}, transport=transport)
        assert output["ok"] is False
        assert "name" in output["error"]

    def test_json_schema_parse_failure_is_a_verdict(self):
        transport = stub_transport(raw=llm_reply("not json at all"))
        with configured():
            output = run({"type": "ai_complete", "prompt": "x",
                          "json_schema": self.SCHEMA}, transport=transport)
        assert output["ok"] is False
        assert "JSON" in output["error"]
        assert output["text"] == "not json at all"

    def test_json_schema_invalid_schema_refuses(self):
        transport = stub_transport()
        with configured():
            for bad in ({"type": "array"}, '{"type": "object"',
                        ["type", "object"], "just a string",
                        {"type": "object", "required": "newsletter"}):
                try:
                    run({"type": "ai_complete", "prompt": "x",
                         "json_schema": bad}, transport=transport)
                except ValueError as exc:
                    assert "json_schema" in str(exc)
                else:
                    raise AssertionError(f"schema {bad!r} must refuse")
        assert transport.calls == []

    def test_json_schema_400_surfaces_as_http_error(self):
        # No silent json_object fallback: a provider rejecting json_schema
        # raises for autoretry like any other HTTP 400.
        transport = stub_transport(status=400, raw=b'{"error": "bad schema"}')
        with configured():
            try:
                run({"type": "ai_complete", "prompt": "x",
                     "json_schema": self.SCHEMA}, transport=transport)
            except HttpError as exc:
                assert exc.status == 400
            else:
                raise AssertionError("HTTP 400 must raise HttpError")

    def test_validate_action_chain_accepts_json_schema(self):
        with configured():
            validate_action_chain([{"type": "ai_complete", "prompt": "hi",
                                    "json_schema": self.SCHEMA}])


class ConfigTests(unittest.TestCase):
    def test_unconfigured_env_fails_with_the_setup_message(self):
        with unconfigured():
            try:
                run({"type": "ai_complete", "prompt": "x"},
                    transport=stub_transport())
            except ValueError as exc:
                message = str(exc)
                assert "ai_complete is not configured" in message
                assert "LLM_API_KEY" in message
                assert "LLM_BASE_URL" in message
                assert "LLM_MODEL" in message
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

    def test_explicit_zero_temperature_is_sent(self):
        transport = stub_transport()
        with configured():
            run({"type": "ai_complete", "prompt": "x",
                 "temperature": 0}, transport=transport)
        assert transport.calls[0]["payload"]["temperature"] == 0

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


class HeadersTests(unittest.TestCase):
    """LLM_HEADERS_JSON carries provider account headers (such as
    Anthropic's anthropic-workspace-id) without putting values into
    committed workflow YAML."""

    def test_extra_headers_merge_over_the_defaults(self):
        transport = stub_transport()
        with configured(LLM_HEADERS_JSON='{"anthropic-workspace-id": "wrkspc_test"}'):
            run({"type": "ai_complete", "prompt": "x"}, transport=transport)
        headers = transport.calls[0]["headers"]
        assert headers["anthropic-workspace-id"] == "wrkspc_test"
        assert headers["authorization"] == "Bearer test-key"
        assert headers["content-type"] == "application/json"

    def test_unset_headers_leave_the_defaults_alone(self):
        transport = stub_transport()
        with configured():
            run({"type": "ai_complete", "prompt": "x"}, transport=transport)
        assert transport.calls[0]["headers"] == {
            "authorization": "Bearer test-key",
            "content-type": "application/json",
            "user-agent": "dapier-ai-complete",
        }

    def test_malformed_headers_refuse_before_any_request(self):
        for bad in ('{"unclosed"', '["not", "an", "object"]', '{"x": 42}'):
            transport = stub_transport()
            with configured(LLM_HEADERS_JSON=bad):
                try:
                    run({"type": "ai_complete", "prompt": "x"},
                        transport=transport)
                except ValueError as exc:
                    assert "LLM_HEADERS_JSON" in str(exc)
                else:
                    raise AssertionError(f"{bad!r} must refuse the step")
            assert transport.calls == []


class AnthropicTests(unittest.TestCase):
    """api.anthropic.com speaks the OpenAI shape but enforces it strictly:
    json_schema needs strict:true plus additionalProperties:false (live
    400s), json_object is rejected, and temperature is deprecated."""

    ANTHROPIC_ENV = {"LLM_BASE_URL": "https://api.anthropic.com/v1"}

    SCHEMA = {
        "type": "object",
        "required": ["newsletter", "sponsors"],
        "properties": {
            "newsletter": {"type": "string"},
            "sponsors": {
                "type": "array",
                "items": {"type": "object",
                          "required": ["name"],
                          "properties": {"name": {"type": "string"}}},
            },
        },
    }

    def run_anthropic(self, action, **env):
        transport = stub_transport(
            raw=llm_reply('{"newsletter": "N", "sponsors": [{"name": "A"}]}'))
        with configured(**{**self.ANTHROPIC_ENV, **env}):
            output = run(action, transport=transport)
        return transport.calls[0], output

    def test_schema_is_normalized_to_the_strict_form(self):
        call, output = self.run_anthropic(
            {"type": "ai_complete", "prompt": "x", "json_schema": self.SCHEMA})
        assert output["ok"] is True
        assert output["data"] == {"newsletter": "N", "sponsors": [{"name": "A"}]}
        response_format = call["payload"]["response_format"]
        assert response_format["type"] == "json_schema"
        sent = response_format["json_schema"]
        assert sent["name"] == "extraction"
        assert sent["strict"] is True
        assert sent["schema"]["additionalProperties"] is False
        items = sent["schema"]["properties"]["sponsors"]["items"]
        assert items["additionalProperties"] is False
        assert "temperature" not in call["payload"]

    def test_normalization_leaves_the_configured_schema_untouched(self):
        schema = {"type": "object",
                  "properties": {"newsletter": {"type": "string"}}}
        self.run_anthropic({"type": "ai_complete", "prompt": "x",
                            "json_schema": schema})
        assert schema == {"type": "object",
                          "properties": {"newsletter": {"type": "string"}}}

    def test_json_mode_sends_no_response_format(self):
        call, output = self.run_anthropic(
            {"type": "ai_complete", "prompt": "judge", "json_mode": True})
        assert "response_format" not in call["payload"]
        assert output["ok"] is True

    def test_other_endpoints_keep_the_generic_payload(self):
        transport = stub_transport(
            raw=llm_reply('{"newsletter": "N", "sponsors": [{"name": "A"}]}'))
        with configured():
            run({"type": "ai_complete", "prompt": "x",
                 "json_schema": self.SCHEMA}, transport=transport)
        sent = transport.calls[0]["payload"]["response_format"]["json_schema"]
        assert "strict" not in sent
        assert "additionalProperties" not in sent["schema"]

    def test_trailing_slash_base_url_still_matches(self):
        with configured(LLM_BASE_URL="https://api.anthropic.com/v1/"):
            assert ai_connector._is_anthropic_endpoint() is True
        with configured():
            assert ai_connector._is_anthropic_endpoint() is False


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
        assert {"name": "ai", "label": "AI", "events": [], "event_info": [], "icon": "sparkles"} \
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
