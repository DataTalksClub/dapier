"""Dry-run field-rule warnings and the strict flag (G10, rendered half).

Save time can only see literals — a ``{token}`` template is exempt there
(data the save has not seen). The dry-run renders those templates against a
sample event, so it re-checks the *rendered* inputs under the same registry
field rules: a required field the sample renders empty (Zapier flags that
instead of sending blanks) and a value that is no longer plausible for its
declared type come back as step ``warnings`` — fatal for the step under
``strict``. The designer save applies the literal rules to every step,
nested branches included.
"""
import json

import pytest
import yaml

from src.dapier.api import designer_store
from src.dapier.api.designer_store import WorkflowError, parse_workflow
from src.dapier.engine import dryrun
from dapier_cli import commands, main


@pytest.fixture
def isolated_home(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.delenv("DAPIER_API_URL", raising=False)
    return tmp_path


WORKFLOW = {
    "id": "wf-strict",
    "enabled": True,
    "trigger": {"connector": "email", "event": "message.received"},
    "actions": [
        {"id": "post", "type": "webhook", "url": "{hook_url}",
         "timeout_seconds": "{secs}"},
    ],
}


def dry_run_one(action, sample, *, strict=False):
    workflow = {
        "id": "wf-strict", "enabled": True,
        "trigger": {"connector": "email", "event": "message.received"},
        "actions": [action],
    }
    report = dryrun.dry_run(workflow, sample, strict=strict)
    assert len(report["steps"]) == 1
    return report["steps"][0], report


# --- warnings, and strict turning them into failures ---


def test_required_field_rendering_empty_warns_but_passes_by_default():
    step, report = dry_run_one({"id": "post", "type": "webhook", "url": "{hook_url}"},
                               {"subject": "hi"})

    assert step["ok"] is True
    assert step["warnings"] == ["required field 'url' renders empty against this sample"]
    assert report["ok"] is True  # warnings ride along unless strict


def test_strict_fails_the_step_on_the_empty_required_field():
    step, report = dry_run_one({"id": "post", "type": "webhook", "url": "{hook_url}"},
                               {"subject": "hi"}, strict=True)

    assert step["ok"] is False
    assert step["error"] == "required field 'url' renders empty against this sample"
    assert report["ok"] is False


def test_rendered_value_implausible_for_its_type_warns():
    step, _report = dry_run_one(
        {"id": "post", "type": "webhook", "url": "https://example.test/hook",
         "timeout_seconds": "{secs}"},
        {"secs": "abc"})

    assert step["ok"] is True
    assert step["warnings"] == ["field 'timeout_seconds' 'abc' is not a number"]


def test_select_options_are_checked_against_the_rendered_value():
    action = {"id": "call", "type": "http_request", "url": "https://example.test",
              "method": "{verb}"}
    step, _report = dry_run_one(action, {"verb": "TELEPORT"})

    assert step["warnings"] == ["field 'method' 'TELEPORT' is not one of: "
                                "DELETE, GET, PATCH, POST, PUT"]

    ok_step, _report = dry_run_one(action, {"verb": "post"})
    assert "warnings" not in ok_step  # case-insensitive, like the runners


def test_well_rendered_steps_carry_no_warnings():
    step, report = dry_run_one(
        {"id": "post", "type": "webhook", "url": "https://example.test/{subject}",
         "timeout_seconds": "{secs}"},
        {"subject": "hi", "secs": "20"})

    assert "warnings" not in step
    assert step["ok"] is True and report["ok"] is True


def test_logic_and_unknown_steps_have_no_field_schema_to_warn_about():
    step, _report = dry_run_one({"id": "gate", "type": "filter",
                                 "field": "subject", "operator": "equals",
                                 "value": "{subject}"}, {"subject": "hi"})
    assert "warnings" not in step

    # Unknown types stay permissive in the dry-run apart from the registry's
    # "unsupported action" verdict (the engine would fail them the same way);
    # either way there is no field schema to warn about.
    step, _report = dry_run_one({"id": "mystery", "type": "mystery_action",
                                 "required_thing": "{nope}"}, {})
    assert "warnings" not in step


def test_test_run_passes_strict_through_to_the_dry_run_only():
    workflow = {**WORKFLOW, "actions": [{"id": "post", "type": "webhook",
                                         "url": "{hook_url}"}]}
    report = dryrun.test_run(workflow, {}, strict=True)
    assert report["steps"][0]["ok"] is False

    execute_report = dryrun.test_run(workflow, {}, execute=True, strict=True)
    assert execute_report["mode"] == "execute"  # strict does not apply here


# --- the API surface: strict rides the test-run body ---


def test_agent_test_run_accepts_strict_and_reports_warnings():
    status, payload = designer_store.api_test_run(None, {
        "workflow": WORKFLOW, "event": {"subject": "hi"}, "strict": True,
    })

    assert status == 200
    step = next(s for s in payload["steps"] if s["action_id"] == "post")
    assert step["ok"] is False
    assert "renders empty" in step["error"]
    assert payload["ok"] is False


def test_agent_test_run_without_strict_keeps_warnings_advisory():
    status, payload = designer_store.api_test_run(None, {
        "workflow": WORKFLOW, "event": {"subject": "hi"},
    })

    assert status == 200
    step = next(s for s in payload["steps"] if s["action_id"] == "post")
    assert step["ok"] is True
    assert step["warnings"] == ["required field 'url' renders empty against this sample"]
    assert payload["ok"] is True


def test_agent_test_run_rejects_a_non_boolean_strict():
    status, payload = designer_store.api_test_run(None, {
        "workflow": WORKFLOW, "event": {}, "strict": "yes",
    })

    assert status == 400
    assert payload["error"] == "strict must be a boolean"


# --- the designer save: literal typed values fail the save, branches too ---


def test_save_rejects_a_literal_wrong_for_its_declared_type():
    with pytest.raises(WorkflowError, match="field 'url'.*is not a URL"):
        parse_workflow(yaml.safe_dump({
            "id": "wf-typed", "trigger": {"connector": "email", "event": "message.received"},
            "actions": [{"id": "post", "type": "webhook", "url": "example.test/hook"}],
        }))


def test_save_rejects_typed_literals_inside_nested_branches():
    workflow = {
        "id": "wf-typed-nested",
        "trigger": {"connector": "email", "event": "message.received"},
        "actions": [{
            "id": "route", "type": "condition", "field": "route", "operator": "equals",
            "value": "invoice",
            "then": [{"id": "bad", "type": "email_send", "to": "not-an-email"}],
        }],
    }
    with pytest.raises(WorkflowError, match="field 'to'.*is not an email address"):
        parse_workflow(yaml.safe_dump(workflow))


def test_save_still_accepts_templates_and_valid_literals():
    workflow = {
        "id": "wf-typed-ok",
        "trigger": {"connector": "email", "event": "message.received"},
        "actions": [
            {"id": "post", "type": "webhook", "url": "{hook_url}", "timeout_seconds": 20},
            {"id": "mail", "type": "email_send", "to": "{sender}",
             "sender": "noreply@example.test", "text": "hi"},
        ],
    }
    assert parse_workflow(yaml.safe_dump(workflow))["id"] == "wf-typed-ok"


def test_validate_field_types_skips_unregistered_and_logic_steps():
    from src.dapier.connectors import registry

    assert registry.validate_field_types({"type": "mystery", "url": 7}, "step 'x'") == {
        "type": "mystery", "url": 7}
    assert registry.validate_field_types({"type": "filter", "value": object()},
                                         "step 'x'")["type"] == "filter"


# --- the CLI: --strict rides the request, warnings print ---


def test_cli_workflows_test_sends_strict_and_prints_warnings(
        isolated_home, monkeypatch, capsys, tmp_path):
    path = tmp_path / "wf.yaml"
    path.write_text(yaml.safe_dump(WORKFLOW), encoding="utf-8")
    calls = []

    def fake_call(api_url, method, endpoint, body=None, **kwargs):
        calls.append((method, endpoint, body))
        return {
            "file": "wf-strict.yaml", "mode": "dry-run", "matched": True,
            "enabled": True, "ok": True,
            "steps": [{"action_id": "post", "action_type": "webhook", "ok": True,
                       "warnings": ["required field 'url' renders empty against this sample"],
                       "rendered_input": {"url": "", "timeout_seconds": ""}}],
        }

    monkeypatch.setattr(commands.api, "call", fake_call)

    rc = main.main(["workflows", "test", str(path), "--event", json.dumps({"a": 1}),
                    "--strict"])

    assert rc == 0
    assert calls[0][2]["strict"] is True and calls[0][2]["execute"] is False
    assert "warning: required field 'url' renders empty" in capsys.readouterr().out
