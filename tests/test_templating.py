"""Step-to-step data mapping and formatters: the execution context, the
template renderer, and the save-time template validation."""
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest

from conftest import stubbed_action
from src.dapier.engine import execute, run_email_send, run_slack
from src.dapier.engine.actions.templating import (
    TemplateError,
    build_context,
    render,
    validate_action,
    validate_template,
)


EVENT = {
    "id": "evt-1",
    "connector": "email",
    "event": "message.received",
    "source": "todo@dtcdev.click",
    "occurred_at": "2026-09-24T15:00:00+00:00",
    "data": {
        "subject": "  Invoice 42  ",
        "amount": "1234.5",
        "route": "todo",
        "sender": {"addresses": ["customer@example.org"]},
    },
}

STEPS = {
    "fetch": {
        "status": "completed",
        "output": {"url": "https://example.test/a", "status": 200, "pi": 3.14159},
    },
}


class ContextTests(unittest.TestCase):
    def test_event_data_sits_at_the_top_level(self):
        context = build_context(EVENT)
        self.assertEqual(context["subject"], "  Invoice 42  ")

    def test_trigger_carries_data_and_envelope_scalars(self):
        context = build_context(EVENT)
        self.assertEqual(context["trigger"]["subject"], "  Invoice 42  ")
        self.assertEqual(context["trigger"]["connector"], "email")
        self.assertEqual(context["trigger"]["id"], "evt-1")
        self.assertEqual(context["trigger"]["occurred_at"], EVENT["occurred_at"])

    def test_non_dict_event_data_degrades_to_empty(self):
        context = build_context({"id": "e", "data": "raw"})
        self.assertEqual(context["trigger"]["data"], {})
        self.assertEqual(render("{subject}", {"data": "raw"}), "")


class RenderTests(unittest.TestCase):
    def test_bare_event_field_still_renders(self):
        self.assertEqual(render("route: {route}", EVENT), "route: todo")

    def test_missing_field_renders_empty_like_before(self):
        self.assertEqual(render("x={nope} y={nope.deep}", EVENT), "x= y=")

    def test_dotted_path_into_event_data(self):
        self.assertEqual(render("{sender.addresses.0}", EVENT), "customer@example.org")

    def test_step_output_path(self):
        self.assertEqual(render("see {steps.fetch.output.url}", EVENT, STEPS),
                         "see https://example.test/a")

    def test_step_status_scalar(self):
        self.assertEqual(render("step: {steps.fetch.status}", EVENT, STEPS), "step: completed")

    def test_unknown_step_and_missing_output_path_render_empty(self):
        self.assertEqual(render("{steps.nope.status}|{steps.fetch.output.missing}", EVENT, STEPS), "|")

    def test_dict_output_renders_as_json(self):
        self.assertEqual(render("{steps.fetch.output}", EVENT, STEPS),
                         '{"url": "https://example.test/a", "status": 200, "pi": 3.14159}')

    def test_trigger_and_bare_fields_resolve_to_the_same_data(self):
        self.assertEqual(render("{trigger.route}/{route}", EVENT), "todo/todo")

    def test_literal_braces_survive(self):
        self.assertEqual(render("{{not a token}}", EVENT), "{not a token}")

    def test_unknown_formatter_renders_empty_with_a_warning_not_a_crash(self):
        self.assertEqual(render("[{subject | frobnicate}]", EVENT), "[]")

    def test_formatter_on_unsuitable_value_renders_empty(self):
        self.assertEqual(render("{route | round:2}", EVENT), "")


class FormatterTests(unittest.TestCase):
    def render_one(self, template, value, steps=None):
        return render(template, {"data": {"v": value}}, steps)

    def test_trim(self):
        self.assertEqual(self.render_one("{v | trim}", "  hi  "), "hi")

    def test_lower_and_upper(self):
        self.assertEqual(self.render_one("{v | lower}", "MiXeD"), "mixed")
        self.assertEqual(self.render_one("{v | upper}", "MiXeD"), "MIXED")

    def test_slice(self):
        self.assertEqual(self.render_one("{v | slice:0:3}", "abcdef"), "abc")
        self.assertEqual(self.render_one("{v | slice:3}", "abcdef"), "def")
        self.assertEqual(self.render_one("{v | slice:-3}", "abcdef"), "def")
        self.assertEqual(self.render_one("{v | slice:2:}", "abcdef"), "cdef")

    def test_replace(self):
        self.assertEqual(self.render_one("{v | replace:a:b}", "banana"), "bbnbnb")

    def test_regex_extract_whole_match_and_group(self):
        self.assertEqual(self.render_one("{v | regex_extract:\\d+}", "a-42-b"), "42")
        self.assertEqual(self.render_one("{v | regex_extract:a-(\\d+)-b}", "a-42-b"), "42")

    def test_regex_extract_without_a_match_renders_empty(self):
        self.assertEqual(self.render_one("{v | regex_extract:xyz}", "abc"), "")

    def test_round_defaults_to_whole_number(self):
        self.assertEqual(self.render_one("{v | round}", "3.7"), "4")

    def test_round_with_digits(self):
        self.assertEqual(self.render_one("{v | round:2}", "3.14159"), "3.14")

    def test_format_number_spec(self):
        self.assertEqual(self.render_one("{v | format:.2f}", "1234.5"), "1234.50")
        self.assertEqual(self.render_one("{v | format:,}", "1234567"), "1,234,567")

    def test_date_format_from_iso_datetime(self):
        self.assertEqual(self.render_one("{v | date_format:%d.%m.%Y}", "2026-09-24T15:00:00+00:00"),
                         "24.09.2026")

    def test_date_format_handles_trailing_z(self):
        self.assertEqual(self.render_one("{v | date_format:%H:%M}", "2026-09-24T15:05:00Z"), "15:05")

    def test_date_format_parses_rfc_2822_email_date_headers(self):
        self.assertEqual(
            self.render_one("{v | date_format:%Y-%m-%d}", "Fri, 26 Sep 2026 22:57:01 +0200"),
            "2026-09-26",
        )

    def test_date_format_unparseable_value_renders_empty(self):
        self.assertEqual(self.render_one("{v | date_format:%Y}", "not a date"), "")

    def test_date_offset_days_and_hours(self):
        self.assertEqual(self.render_one("{v | date_offset:1d}", "2026-09-24T15:00:00+00:00"),
                         "2026-09-25T15:00:00+00:00")
        self.assertEqual(self.render_one("{v | date_offset:-2h}", "2026-09-24T15:00:00+00:00"),
                         "2026-09-24T13:00:00+00:00")

    def test_formatters_chain_left_to_right(self):
        self.assertEqual(
            self.render_one("{v | date_offset:1w | date_format:%Y-%m-%d}", "2026-09-24"),
            "2026-10-01",
        )

    def test_cross_step_formatter(self):
        self.assertEqual(render("{steps.fetch.output.pi | round:2}", EVENT, STEPS), "3.14")
        self.assertEqual(render("{trigger.subject | trim | upper}", EVENT), "INVOICE 42")

    def test_default_falls_back_on_empty(self):
        self.assertEqual(self.render_one("{v | default:anon}", ""), "anon")
        self.assertEqual(self.render_one("{v | default:anon}", "   "), "anon")

    def test_default_keeps_a_present_value(self):
        self.assertEqual(self.render_one("{v | default:anon}", "sam"), "sam")

    def test_default_covers_a_missing_field(self):
        self.assertEqual(render("{nope | default:fallback}", EVENT), "fallback")

    def test_default_value_may_contain_colons(self):
        self.assertEqual(self.render_one("{v | default:http://x.test}", ""), "http://x.test")

    def test_number_format_adds_thousands_separators(self):
        self.assertEqual(self.render_one("{v | number_format}", "1234567"), "1,234,567")

    def test_number_format_with_decimal_places(self):
        self.assertEqual(self.render_one("{v | number_format:2}", "1234567.891"), "1,234,567.89")

    def test_number_format_on_non_numeric_renders_empty(self):
        self.assertEqual(self.render_one("{v | number_format}", "lots"), "")


class FormatterEdgeTests(unittest.TestCase):
    """The formatter edges: timezone-aware date_format, relative time_until,
    and number_format's currency mode — each error-tolerant like the rest."""

    def render_one(self, template, value):
        return render(template, {"data": {"v": value}})

    def test_date_format_renders_in_an_iana_zone(self):
        self.assertEqual(
            self.render_one("{v | date_format:%Y-%m-%d %H:%M@Europe/Berlin}",
                            "2026-09-24T15:00:00+00:00"),
            "2026-09-24 17:00",
        )

    def test_date_format_zone_keeps_the_colon_in_the_format_spec(self):
        self.assertEqual(
            self.render_one("{v | date_format:%H:%M@Europe/Berlin}",
                            "2026-09-24T15:05:00+00:00"),
            "17:05",
        )

    def test_date_format_zone_reads_a_naive_value_as_utc(self):
        self.assertEqual(
            self.render_one("{v | date_format:%H:%M@America/New_York}",
                            "2026-09-24T15:05:00"),
            "11:05",
        )

    def test_date_format_unknown_zone_renders_empty_not_a_crash(self):
        self.assertEqual(
            self.render_one("{v | date_format:%Y@Mars/Olympus}", "2026-09-24T15:00:00Z"), "")

    def test_time_until_future_renders_in_the_unit(self):
        self.assertEqual(
            self.render_one("{v | time_until}",
                            (datetime.now(timezone.utc) + timedelta(hours=3)).isoformat()),
            "in 3h")

    def test_time_until_past_renders_ago(self):
        self.assertEqual(
            self.render_one("{v | time_until}",
                            (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()),
            "5m ago")

    def test_time_until_picks_the_largest_unit(self):
        self.assertEqual(
            self.render_one("{v | time_until}",
                            (datetime.now(timezone.utc) + timedelta(days=2)).isoformat()),
            "in 2d")

    def test_time_until_within_a_minute_is_just_now(self):
        self.assertEqual(
            self.render_one("{v | time_until}",
                            (datetime.now(timezone.utc) + timedelta(seconds=10)).isoformat()),
            "just now")

    def test_time_until_reads_a_trailing_z_and_a_naive_value_as_utc(self):
        zulu = datetime.now(timezone.utc).replace(microsecond=0) + timedelta(days=2)
        self.assertEqual(self.render_one("{v | time_until}", zulu.strftime("%Y-%m-%dT%H:%M:%SZ")),
                         "in 2d")
        naive = datetime.now(timezone.utc).replace(microsecond=0, tzinfo=None)
        self.assertEqual(self.render_one("{v | time_until}", naive.isoformat()), "just now")

    def test_time_until_unparseable_value_renders_empty(self):
        self.assertEqual(self.render_one("{v | time_until}", "not a date"), "")

    def test_number_format_currency_prefixes_the_symbol(self):
        self.assertEqual(self.render_one("{v | number_format:currency:EUR}", "1234.56"), "€1,234.56")
        self.assertEqual(self.render_one("{v | number_format:currency:USD}", "1234.56"), "$1,234.56")
        self.assertEqual(self.render_one("{v | number_format:currency:GBP}", "1234.56"), "£1,234.56")

    def test_number_format_unknown_currency_falls_back_to_the_code(self):
        self.assertEqual(self.render_one("{v | number_format:currency:JPY}", "1234.56"),
                         "1,234.56 JPY")

    def test_number_format_currency_decimal_override(self):
        self.assertEqual(self.render_one("{v | number_format:currency:USD:0}", "1234.56"),
                         "$1,235")

    def test_number_format_currency_without_a_code_renders_empty(self):
        self.assertEqual(self.render_one("{v | number_format:currency}", "1234.56"), "")

    def test_new_grammars_pass_save_time_validation(self):
        validate_template("{v | date_format:%H:%M@Europe/Berlin} {w | time_until} "
                          "{n | number_format:currency:EUR:2} {m | number_format:2}")

    def test_currency_mode_without_a_code_fails_save_time_validation(self):
        with pytest.raises(TemplateError):
            validate_template("{n | number_format:currency}")

    def test_number_format_currency_on_non_numeric_renders_empty(self):
        self.assertEqual(self.render_one("{v | number_format:currency:EUR}", "lots"), "")


class ValidateTests(unittest.TestCase):
    def test_accepts_known_formatters_and_plain_templates(self):
        for template in ["plain text", "{a}", "{a.b.0.c}", "{a | trim | upper}",
                         "{a | slice:-5:}", "{a | round:2}", "{a | format:.2f}",
                         "{a | regex_extract:(\\d+):end}", "{a | date_offset:-2h}",
                         "{a | default:x}", "{a | number_format:2}", "{a | number_format}",
                         "{a | date_format:%Y-%m-%d %H:%M@Europe/Berlin}", "{a | time_until}",
                         "{a | number_format:currency:EUR}", "{a | number_format:currency:USD:0}"]:
            validate_template(template)  # must not raise

    def test_rejects_unknown_formatter(self):
        with pytest.raises(TemplateError, match="unknown formatter 'nope'"):
            validate_template("{a | nope}")

    def test_rejects_empty_path(self):
        with pytest.raises(TemplateError, match="empty path"):
            validate_template("{ | trim}")

    def test_rejects_wrong_argument_counts(self):
        for template in ["{a | trim:1}", "{a | replace:x}", "{a | slice}", "{a | upper:x}",
                         "{a | default}", "{a | number_format:1:2}", "{a | number_format:currency}"]:
            with pytest.raises(TemplateError):
                validate_template(template)

    def test_validate_action_names_the_field(self):
        with pytest.raises(TemplateError, match=r"\btext\b"):
            validate_action({"type": "email_send", "text": "x {a | nope}"})


class ExecuteMappingTests(unittest.TestCase):
    """execute() accumulates step outputs; later steps template against them."""

    WORKFLOW = {
        "id": "wf-map", "enabled": True,
        "trigger": {"connector": "email", "event": "message.received", "filters": {}},
        "actions": [
            {"id": "fetch", "type": "webhook", "url": "https://example.test/fetch"},
            {"id": "notify", "type": "slack", "credential_id": "slack", "channel": "C1",
             "text": "{steps.fetch.output.url | upper} ({steps.fetch.status})"},
        ],
    }
    EVENT = {"id": "evt-9", "connector": "email", "event": "message.received",
             "data": {"route": "todo"}}

    def test_second_step_reads_the_first_steps_output(self):
        with patch("src.dapier.engine.all_workflows", return_value=[self.WORKFLOW]), \
             stubbed_action("webhook", lambda action, event: {"url": "https://x/y"}), \
             patch("src.dapier.engine.actions.base._json_request") as json_request, \
             patch("src.dapier.connections.credentials.get_credential", return_value={"token": "t"}):
            execute(dict(self.EVENT))
        text = json_request.call_args.args[1]["text"]
        self.assertEqual(text, "HTTPS://X/Y (completed)")

    def test_skipped_step_is_recorded_with_its_status(self):
        def before_action(_wf, action_id, _event, _type):
            return action_id != "fetch"  # lease held elsewhere: skip the first step

        with patch("src.dapier.engine.all_workflows", return_value=[self.WORKFLOW]), \
             stubbed_action("webhook", lambda action, event: {"url": "u"}), \
             patch("src.dapier.engine.actions.base._json_request") as json_request, \
             patch("src.dapier.connections.credentials.get_credential", return_value={"token": "t"}):
            execute(dict(self.EVENT), before_action=before_action)
        self.assertIn("(skipped)", json_request.call_args.args[1]["text"])

    def test_each_workflow_run_starts_with_fresh_steps(self):
        workflows = [self.WORKFLOW, dict(self.WORKFLOW, id="wf-map-2")]
        texts = []
        with patch("src.dapier.engine.all_workflows", return_value=workflows), \
             stubbed_action("webhook", lambda action, event: {"url": "u"}), \
             patch("src.dapier.engine.actions.base._json_request") as json_request, \
             patch("src.dapier.connections.credentials.get_credential", return_value={"token": "t"}):
            execute(dict(self.EVENT))
        for call in json_request.call_args_list:
            texts.append(call.args[1]["text"])
        self.assertEqual(texts, ["U (completed)", "U (completed)"])


class RunnerMappingTests(unittest.TestCase):
    def test_email_send_renders_step_output(self):
        sent = []

        class Ses:
            def send_email(self, **kwargs):
                sent.append(kwargs)
                return {"MessageId": "mid"}

        action = {"type": "email_send", "to": "ops@example.com",
                  "subject": "pdf ready", "text": "{steps.render.output.s3.key}"}
        event = {"id": "e", "connector": "email", "data": {}}
        steps = {"render": {"status": "completed",
                            "output": {"s3": {"bucket": "b", "key": "rendered/1.pdf"}}}}
        run_email_send(action, event, ses=Ses(), steps=steps)
        self.assertEqual(sent[0]["Message"]["Body"]["Text"]["Data"], "rendered/1.pdf")

    def test_slack_renders_formatter_chain(self):
        with patch("src.dapier.engine.actions.base._json_request") as json_request, \
             patch("src.dapier.connections.credentials.get_credential", return_value={"token": "t"}):
            run_slack({"credential_id": "s", "channel": "C1", "text": "{subject | trim | upper}"},
                      {"data": {"subject": " hi "}})
        self.assertEqual(json_request.call_args.args[1]["text"], "HI")


class SaveValidationTests(unittest.TestCase):
    """Templates are checked loudly when a workflow or trigger is saved."""

    def test_designer_save_rejects_unknown_formatter(self):
        from src.dapier.api import designer_store

        yaml_text = (
            "id: bad-template\n"
            "trigger: {connector: email, event: message.received}\n"
            "actions:\n"
            "  - type: email_send\n"
            "    to: ops@example.com\n"
            "    text: '{subject | frobnicate}'\n"
        )
        with pytest.raises(designer_store.WorkflowError, match="unknown formatter 'frobnicate'"):
            designer_store.parse_workflow(yaml_text)

    def test_designer_save_accepts_step_and_formatter_templates(self):
        from src.dapier.api import designer_store

        yaml_text = (
            "id: good-template\n"
            "trigger: {connector: email, event: message.received}\n"
            "actions:\n"
            "  - type: email_send\n"
            "    to: ops@example.com\n"
            "    subject: '{trigger.subject | trim}'\n"
            "    text: '{steps.render.output.s3.key | upper}'\n"
        )
        designer_store.parse_workflow(yaml_text)  # must not raise

    def test_stored_trigger_save_rejects_unknown_formatter(self):
        from src.dapier.triggers import email_triggers

        with pytest.raises(email_triggers.TriggerError, match="unknown formatter 'nope'"):
            email_triggers.validate_actions(
                [{"type": "slack", "credential_id": "s", "channel": "C1",
                  "text": "{subject | nope}"}],
            )


if __name__ == "__main__":
    unittest.main()


class FormatterBreadthTests(unittest.TestCase):
    """The Zapier-Formatter breadth beyond trim/upper/replace."""

    def test_split_picks_an_element(self):
        self.assertEqual(render("{path | split:/:1}", {"data": {"path": "a/b/c"}}), "b")

    def test_split_out_of_range_renders_empty(self):
        self.assertEqual(render("{path | split:/:9}", {"data": {"path": "a/b"}}), "")

    def test_join_flattens_a_json_list(self):
        self.assertEqual(render("{tags | join:;}", {"data": {"tags": ["a", "b"]}}), "a;b")

    def test_join_passes_non_lists_through(self):
        self.assertEqual(render("{name | join:-}", {"data": {"name": "solo"}}), "solo")

    def test_title_capitalizes_words(self):
        self.assertEqual(render("{name | title}", {"data": {"name": "hello world"}}), "Hello World")

    def test_urlencode_escapes_for_urls(self):
        self.assertEqual(render("{q | urlencode}", {"data": {"q": "a b&c"}}), "a+b%26c")

    def test_length_counts_strings_and_lists(self):
        self.assertEqual(render("{name | length}", {"data": {"name": "abcd"}}), "4")
        self.assertEqual(render("{items | length}", {"data": {"items": [1, 2, 3]}}), "3")

    def test_truncate_keeps_the_limit_including_the_suffix(self):
        self.assertEqual(render("{text | truncate:5}", {"data": {"text": "abcdefg"}}), "abcd…")

    def test_truncate_leaves_short_values_alone(self):
        self.assertEqual(render("{text | truncate:10}", {"data": {"text": "short"}}), "short")

    def test_slugify_normalizes_a_heading(self):
        self.assertEqual(render("{t | slugify}", {"data": {"t": "Hello, Zapier World!"}}),
                         "hello-zapier-world")

    def test_arithmetic_formatters_collapse_integral_results(self):
        self.assertEqual(render("{n | add:2}", {"data": {"n": "40"}}), "42")
        self.assertEqual(render("{n | subtract:2}", {"data": {"n": "40"}}), "38")
        self.assertEqual(render("{n | multiply:3}", {"data": {"n": "7"}}), "21")
        self.assertEqual(render("{n | divide:4}", {"data": {"n": "10"}}), "2.5")

    def test_divide_by_zero_renders_empty_like_any_formatter_failure(self):
        self.assertEqual(render("{n | divide:0}", {"data": {"n": "10"}}), "")

    def test_new_formatters_pass_save_time_validation(self):
        validate_template("{a | split:,:0 | title | truncate:80} {b | urlencode | slugify} "
                          "{c | length} {d | join:-} {e | add:1}")
