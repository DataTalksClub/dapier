"""Validation of the generic per-step error-handling keys (gap G3).

``on_error: halt|continue|run`` and ``error_actions`` are legal on every
step — connector actions and logic steps alike. Both validation layers must
accept the keys and reject bad shapes at save time: the connector registry
for stored trigger chains and designer_store's structural validation for
designer saves (which also recurses into error_actions as a nested chain).
"""
import re

import pytest

from src.dapier.api import designer_store
from src.dapier.connectors import registry


def webhook_chain(**extra):
    action = {"type": "webhook", "url": "https://example.test/hook"}
    action.update(extra)
    return [action]


def test_error_modes_match_the_engine():
    """registry.ON_ERROR_MODES and logic.ERROR_MODES are deliberately
    duplicated (registry must not import the engine package); this pins the
    "keep in sync" comment so the two can only drift together with a test
    failure."""
    from src.dapier.engine import logic

    assert registry.ON_ERROR_MODES == logic.ERROR_MODES
    assert registry.ON_FAIL_MODES == logic.ON_FAIL_MODES


def workflow_yaml(actions_yaml):
    return f"""\
id: test-flow
trigger:
  connector: email
  event: message.received
actions:
{actions_yaml}
"""


# --- registry layer: stored trigger chains ---------------------------------


def test_chain_accepts_on_error_modes():
    fallback = [{"type": "webhook", "url": "https://example.test/fallback"}]
    for mode in registry.ON_ERROR_MODES:
        extra = {"on_error": mode}
        if mode == "run":
            extra["error_actions"] = fallback
        assert registry.validate_action_chain(webhook_chain(**extra)) == [
            dict({"type": "webhook", "url": "https://example.test/hook"}, **extra)
        ]


def test_chain_accepts_run_with_error_actions():
    chain = webhook_chain(
        on_error="run",
        error_actions=[{"type": "webhook", "url": "https://example.test/fallback"}],
    )
    assert registry.validate_action_chain(chain) == chain


def test_chain_rejects_unknown_on_error_mode():
    with pytest.raises(registry.ActionError, match="on_error must be one of halt, continue, run"):
        registry.validate_action_chain(webhook_chain(on_error="contine"))


def test_chain_rejects_run_without_error_actions():
    with pytest.raises(registry.ActionError, match="error_actions must be a non-empty list"):
        registry.validate_action_chain(webhook_chain(on_error="run"))


def test_chain_rejects_error_actions_outside_run_mode():
    for mode in ("halt", "continue"):
        with pytest.raises(registry.ActionError, match="error_actions only run when on_error is run"):
            registry.validate_action_chain(webhook_chain(on_error=mode, error_actions=[
                {"type": "webhook", "url": "https://example.test/fallback"}]))


def test_chain_rejects_malformed_error_actions():
    with pytest.raises(registry.ActionError, match="error_actions must be a non-empty list"):
        registry.validate_action_chain(webhook_chain(on_error="run", error_actions=[]))
    with pytest.raises(registry.ActionError, match="error_actions must be a non-empty list"):
        registry.validate_action_chain(webhook_chain(on_error="run", error_actions="fallback"))


def test_chain_validates_error_actions_as_a_chain():
    with pytest.raises(registry.ActionError, match="unsupported action type: 'mystery'"):
        registry.validate_action_chain(webhook_chain(on_error="run", error_actions=[
            {"type": "mystery"}]))
    with pytest.raises(registry.ActionError, match="has unknown keys: nope"):
        registry.validate_action_chain(webhook_chain(on_error="run", error_actions=[
            {"type": "webhook", "url": "https://example.test/fallback", "nope": 1}]))


# --- designer layer: parse_workflow / _validate_steps -----------------------


def test_parse_accepts_error_keys_on_actions_and_logic_steps():
    workflow = designer_store.parse_workflow(workflow_yaml(
        "  - id: a1\n"
        "    type: webhook\n"
        "    url: https://example.test/hook\n"
        "    on_error: continue\n"
        "  - id: f1\n"
        "    type: filter\n"
        "    field: subject\n"
        "    on_error: run\n"
        "    error_actions:\n"
        "      - id: a2\n"
        "        type: webhook\n"
        "        url: https://example.test/fallback\n"
    ))
    assert workflow["actions"][0]["on_error"] == "continue"
    assert workflow["actions"][1]["error_actions"][0]["id"] == "a2"


def test_parse_treats_empty_on_error_as_unset():
    workflow = designer_store.parse_workflow(workflow_yaml(
        "  - id: a1\n"
        "    type: webhook\n"
        "    url: https://example.test/hook\n"
        '    on_error: ""\n'
    ))
    assert workflow["actions"][0]["on_error"] == ""


@pytest.mark.parametrize("yaml_text,fragment", [
    (workflow_yaml("  - id: a1\n    type: webhook\n    url: https://example.test/hook\n"
                   "    on_error: contine\n"),
     "step 'a1': on_error must be one of halt, continue, run"),
    (workflow_yaml("  - id: a1\n    type: webhook\n    url: https://example.test/hook\n"
                   "    on_error: run\n"),
     "step 'a1': error_actions must be a non-empty list"),
    (workflow_yaml("  - id: a1\n    type: webhook\n    url: https://example.test/hook\n"
                   "    on_error: continue\n    error_actions: []\n"),
     "step 'a1': error_actions must be a non-empty list"),
    (workflow_yaml("  - id: a1\n    type: webhook\n    url: https://example.test/hook\n"
                   "    error_actions:\n      - type: webhook\n        url: https://example.test/f\n"),
     "step 'a1': error_actions only run when on_error is run"),
    # Logic steps are validated too, and error_actions recurses as a chain.
    (workflow_yaml("  - id: f1\n    type: filter\n    field: subject\n    on_error: nope\n"),
     "step 'f1': on_error must be one of halt, continue, run"),
    (workflow_yaml("  - id: a1\n    type: webhook\n    url: https://example.test/hook\n"
                   "    on_error: run\n    error_actions:\n"
                   "      - id: bail\n        type: webhook\n        on_error: contine\n"),
     "step 'bail': on_error must be one of halt, continue, run"),
    (workflow_yaml("  - id: a1\n    type: webhook\n    url: https://example.test/hook\n"
                   "    on_error: run\n    error_actions:\n      - id: broken\n"),
     "a1.error[0]: every step needs a type"),
])
def test_parse_rejects_bad_error_key_shapes(yaml_text, fragment):
    with pytest.raises(designer_store.WorkflowError, match=re.escape(fragment)):
        designer_store.parse_workflow(yaml_text)


# --- designer layer: the sibling on_fail policy validates at save too --------


def test_parse_accepts_on_fail_mode():
    workflow = designer_store.parse_workflow(workflow_yaml(
        "  - id: a1\n"
        "    type: webhook\n"
        "    url: https://example.test/hook\n"
        "    on_fail: continue\n"
    ))
    assert workflow["actions"][0]["on_fail"] == "continue"


@pytest.mark.parametrize("yaml_text,fragment", [
    (workflow_yaml("  - id: a1\n    type: webhook\n    url: https://example.test/hook\n"
                   "    on_fail: nope\n"),
     "step 'a1': on_fail must be one of continue, halt"),
    (workflow_yaml("  - id: a1\n    type: webhook\n    url: https://example.test/hook\n"
                   "    on_error: continue\n    on_fail: continue\n"),
     "step 'a1': set on_fail or on_error, not both"),
    # The error branch recurses as a chain, so on_fail is checked there too.
    (workflow_yaml("  - id: a1\n    type: webhook\n    url: https://example.test/hook\n"
                   "    on_error: run\n    error_actions:\n"
                   "      - id: bail\n        type: webhook\n"
                   "        url: https://example.test/f\n"
                   "        on_fail: continue\n        on_error: continue\n"),
     "step 'bail': set on_fail or on_error, not both"),
])
def test_parse_rejects_bad_on_fail_shapes(yaml_text, fragment):
    with pytest.raises(designer_store.WorkflowError, match=re.escape(fragment)):
        designer_store.parse_workflow(yaml_text)
