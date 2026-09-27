"""Save-time registry validation: error-handling keys and typed field literals.

Covers the generic ``on_error``/``error_actions`` step keys (including
recursion into the error chain) and the ``type: number|email|url`` field
checks — literal values only, templated values skipped.
"""
import unittest
from contextlib import contextmanager

import pytest

from src.dapier.connectors import registry
from src.dapier.connectors.registry import Action, ActionError, validate_action_chain


WEBHOOK = {"type": "webhook", "url": "https://example.test/hook"}


@contextmanager
def registered(action):
    """Register a scratch action so field-type checks have something to bite."""
    registry.ACTIONS[action.type] = action
    try:
        yield
    finally:
        registry.ACTIONS.pop(action.type, None)


TYPED_PROBE = Action(
    type="typed_probe", label="Probe", run=lambda *args, **kwargs: {},
    required=frozenset({"url"}),
    optional=frozenset({"retries", "contact", "detail"}),
    fields=(
        {"key": "url", "label": "URL", "type": "url", "required": True},
        {"key": "retries", "label": "Retries", "type": "number"},
        {"key": "contact", "label": "Contact", "type": "email"},
        {"key": "detail", "label": "Detail", "type": "textarea"},
    ),
)


class ErrorKeyValidationTests(unittest.TestCase):
    def test_error_keys_are_accepted_and_recursed(self):
        chain = [{**WEBHOOK, "on_error": "run",
                  "error_actions": [{**WEBHOOK, "id": "alert"}]}]
        assert validate_action_chain(chain) is chain

    def test_error_actions_are_validated_recursively(self):
        with pytest.raises(ActionError, match="bogus"):
            validate_action_chain([
                {**WEBHOOK, "on_error": "run",
                 "error_actions": [{**WEBHOOK, "bogus": 1}]},
            ])

    def test_nested_error_actions_may_nest_again(self):
        validate_action_chain([
            {**WEBHOOK, "on_error": "run",
             "error_actions": [{**WEBHOOK, "on_error": "continue"}]},
        ])

    def test_invalid_mode_fails_the_save(self):
        with pytest.raises(ActionError, match="on_error must be one of"):
            validate_action_chain([{**WEBHOOK, "on_error": "explode"}])

    def test_error_actions_need_a_non_empty_list(self):
        with pytest.raises(ActionError, match="non-empty list"):
            validate_action_chain([{**WEBHOOK, "on_error": "run", "error_actions": []}])
        with pytest.raises(ActionError, match="non-empty list"):
            validate_action_chain([{**WEBHOOK, "on_error": "run",
                                    "error_actions": "alert"}])

    def test_error_actions_only_run_under_on_error_run(self):
        with pytest.raises(ActionError, match="only run when on_error is run"):
            validate_action_chain([{**WEBHOOK, "on_error": "continue",
                                    "error_actions": [{**WEBHOOK}]}])


class FieldTypeValidationTests(unittest.TestCase):
    def test_valid_literals_pass_and_return_the_chain(self):
        chain = [{"type": "typed_probe", "url": "https://example.test/hook",
                  "retries": 3, "contact": "ops@example.com"}]
        with registered(TYPED_PROBE):
            assert validate_action_chain(chain) is chain

    def test_numeric_strings_are_accepted(self):
        chain = [{"type": "typed_probe", "url": "https://example.test",
                  "retries": "3"}]
        with registered(TYPED_PROBE):
            validate_action_chain(chain)

    def test_invalid_number_fails_the_save(self):
        for bad in ("abc", True, "1.2.3"):
            with registered(TYPED_PROBE), pytest.raises(ActionError, match="not a number"):
                validate_action_chain([{"type": "typed_probe",
                                        "url": "https://example.test",
                                        "retries": bad}])

    def test_invalid_email_fails_the_save(self):
        with registered(TYPED_PROBE), pytest.raises(ActionError, match="not an email"):
            validate_action_chain([{"type": "typed_probe", "url": "https://example.test",
                                    "contact": "not-an-email"}])

    def test_invalid_url_fails_the_save(self):
        with registered(TYPED_PROBE), pytest.raises(ActionError, match="not a URL"):
            validate_action_chain([{"type": "typed_probe", "url": "example.test/no-scheme"}])

    def test_templated_values_are_skipped(self):
        chain = [{"type": "typed_probe", "url": "https://example.test/{path}",
                  "retries": "{cfg.retries}", "contact": "{cfg.contact}"}]
        with registered(TYPED_PROBE):
            validate_action_chain(chain)

    def test_empty_and_absent_values_are_skipped(self):
        chain = [{"type": "typed_probe", "url": "https://example.test",
                  "retries": "", "detail": "plain text is never type-checked"}]
        with registered(TYPED_PROBE):
            validate_action_chain(chain)

    def test_real_registry_number_field(self):
        # timeout_seconds is the live typed field: numeric literals pass,
        # typos fail, templates are skipped.
        validate_action_chain([{**WEBHOOK, "timeout_seconds": 30}])
        validate_action_chain([{**WEBHOOK, "timeout_seconds": "15.5"}])
        validate_action_chain([{**WEBHOOK, "timeout_seconds": "{cfg.timeout}"}])
        with pytest.raises(ActionError, match="timeout_seconds.*not a number"):
            validate_action_chain([{**WEBHOOK, "timeout_seconds": "abc"}])


if __name__ == "__main__":
    pytest.main([__file__])
