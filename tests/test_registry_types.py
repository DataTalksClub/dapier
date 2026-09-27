"""Save-time field-type validation (registry FIELD_TYPES).

validate_action_chain rejects *literal* values that are clearly wrong for a
field's declared ``type`` — number, integer, boolean, email, url, or enum
(``choices``, with a ``select`` field's ``options`` as the designer-widget
alias). Values carrying a ``{token}`` template are exempt: they resolve at
run time from data the save has not seen. The checks are deliberately
shallow — enum membership compares case-insensitively because every runner
normalizes case before checking — so existing chains keep saving.
"""
import pytest

from src.dapier.connectors import registry
from src.dapier.connectors.registry import ActionError, validate_action_chain


def chain(action_type, field_type, value, *, key="value", choices=None, extra=None):
    """A one-step chain against a synthetic action typed for the test."""
    field = {"key": key, "label": "Value", "type": field_type}
    if choices is not None:
        field["choices"] = choices
    entry = registry.Action(
        type="typed_test_action",
        label="Typed test action",
        run=lambda action, event, workflow_id, steps=None: {},
        required=frozenset(),
        optional=frozenset({key}),
        fields=(field,),
    )
    registry.ACTIONS[entry.type] = entry
    try:
        if extra is None:
            return validate_action_chain([{"type": entry.type, key: value}])
        with pytest.raises(ActionError) as caught:
            validate_action_chain([{"type": entry.type, key: value}])
        return caught
    finally:
        del registry.ACTIONS[entry.type]


# --- each type accepts its own literals and rejects the wrong ones ---


@pytest.mark.parametrize("value", [15, 15.5, "15", "15.5", " 15 ", -3])
def test_number_accepts_numbers(value):
    assert chain("typed", "number", value)


@pytest.mark.parametrize("value", ["abc", "1,000", True])
def test_number_rejects_non_numbers(value):
    caught = chain("typed", "number", value, extra=True)
    assert "is not a number" in str(caught.value)


@pytest.mark.parametrize("value", ["", "  "])
def test_number_skips_empty_values_required_keys_own_those(value):
    assert chain("typed", "number", value)


@pytest.mark.parametrize("value", [3, "3", -1, " 2 "])
def test_integer_accepts_whole_numbers(value):
    assert chain("typed", "integer", value)


@pytest.mark.parametrize("value", [2.5, "2.5", "abc", True])
def test_integer_rejects_fractions_and_words(value):
    caught = chain("typed", "integer", value, extra=True)
    assert "is not a" in str(caught.value)


@pytest.mark.parametrize("value", [True, False, "true", "False", "yes", "off", 1, "0"])
def test_boolean_accepts_runner_shapes(value):
    assert chain("typed", "boolean", value)


@pytest.mark.parametrize("value", ["ture", "maybe", 2, "enabled"])
def test_boolean_rejects_typos(value):
    caught = chain("typed", "boolean", value, extra=True)
    assert "is not a boolean" in str(caught.value)


@pytest.mark.parametrize("value", [
    "ops@example.com", "OPS@EXAMPLE.IO", "a@x.com, b@y.org",  # run_email_send splits on commas
])
def test_email_accepts_addresses(value):
    assert chain("typed", "email", value)


@pytest.mark.parametrize("value", ["nope", "a@x", "ops at example.com", "a@x.com,b@y"])
def test_email_rejects_non_addresses(value):
    caught = chain("typed", "email", value, extra=True)
    assert "is not an email address" in str(caught.value)


@pytest.mark.parametrize("value", [
    "https://example.test/hook", "http://localhost:5000/x", "s3://bucket/key",
])
def test_url_accepts_urls(value):
    assert chain("typed", "url", value)


@pytest.mark.parametrize("value", ["example.test", "ftp only", "//no-scheme.test"])
def test_url_rejects_non_urls(value):
    caught = chain("typed", "url", value, extra=True)
    assert "is not a URL" in str(caught.value)


# --- enum: declared choices, and the select-widget alias ---


def test_enum_accepts_declared_choices_and_rejects_the_rest():
    assert chain("typed", "enum", "user", choices=["user", "channel"])
    caught = chain("typed", "enum", "SMTP", choices=["GET", "POST"], extra=True)
    assert "'SMTP' is not one of: GET, POST" in str(caught.value)


def test_enum_compares_case_insensitively_like_the_runners():
    assert chain("typed", "enum", "get", choices=["GET", "POST"])
    assert chain("typed", "enum", "USER_entered", choices=["USER_ENTERED", "RAW"])


def test_select_options_count_as_choices():
    field_entry = registry.Action(
        type="typed_select_action",
        label="Typed select action",
        run=lambda action, event, workflow_id, steps=None: {},
        optional=frozenset({"kind"}),
        fields=({"key": "kind", "label": "Kind", "type": "select",
                 "options": ["any", "file", "folder"]},),
    )
    registry.ACTIONS[field_entry.type] = field_entry
    try:
        assert validate_action_chain([{"type": field_entry.type, "kind": "folder"}])
        with pytest.raises(ActionError, match="is not one of: any, file, folder"):
            validate_action_chain([{"type": field_entry.type, "kind": "dir"}])
    finally:
        del registry.ACTIONS[field_entry.type]


# --- template exemption and the quiet cases ---


@pytest.mark.parametrize("value", [
    "{trigger.url}", "https://api.test/{id}/x", "{sender}", "pre {a} post",
])
def test_templates_are_exempt_from_their_declared_type(value):
    assert chain("typed", "url", value)
    assert chain("typed", "email", value)
    assert chain("typed", "enum", value, choices=["alpha"])


def test_missing_and_structural_values_are_not_checked():
    assert validate_action_chain([{"type": "webhook", "url": "https://x.test"}])
    entry = registry.ACTIONS["webhook"]
    assert registry._validate_field_types(
        {"type": "webhook", "timeout_seconds": ["not", "a", "number"]}, entry) is None
    assert registry._validate_field_types({"type": "webhook"}, entry) is None


# --- the error names the step and the field ---


def test_error_names_action_type_and_field():
    with pytest.raises(ActionError) as caught:
        validate_action_chain([{"type": "email_send", "to": "not-an-email", "text": "x"}])
    assert "email_send action field 'to'" in str(caught.value)


# --- the real annotations across the connectors ---


def test_real_connector_fields_carry_the_declared_types():
    def field(action_type, key):
        entry = registry.ACTIONS[action_type]
        return next(field for field in entry.fields if field.get("key") == key)

    assert field("webhook", "url")["type"] == "url"
    assert field("http_request", "url")["type"] == "url"
    assert field("http_request", "method")["options"] == list(registry.ACTIONS[
        "http_request"].fields[1]["options"])  # select keeps its options
    assert field("dataops", "url")["type"] == "url"
    assert field("s3_upload", "source_url")["type"] == "url"
    assert field("email_send", "to")["type"] == "email"
    assert field("email_send", "sender")["type"] == "email"
    assert field("slack_find_user", "email")["type"] == "email"
    for action_type in ("code", "js", "webhook", "http_request", "slack",
                        "telegram_send", "dataops"):
        assert field(action_type, "timeout_seconds")["type"] == "number"
    for action_type, key in (
        ("sheets_find_row", "create_if_missing"),
        ("dropbox_find", "create_if_missing"),
        ("slack", "unfurl_links"),
        ("render_html_to_pdf", "print_background"),
    ):
        assert field(action_type, key)["type"] == "boolean"


def test_annotated_chains_still_validate():
    validate_action_chain([
        {"type": "email_send", "to": "ops@example.com, alerts@example.com",
         "sender": "noreply@example.test", "text": "hi"},
        {"type": "webhook", "url": "https://example.test/hook", "timeout_seconds": 20},
        {"type": "http_request", "url": "{trigger.url}", "method": "POST"},
        {"type": "slack_find_user", "connection_id": "slack",
         "email": "{steps.lookup.output.values.0}"},
    ])
    with pytest.raises(ActionError, match="is not a URL"):
        validate_action_chain([{"type": "webhook", "url": "example.test/hook"}])
    with pytest.raises(ActionError, match="is not an email address"):
        validate_action_chain([{"type": "email_send", "to": "ops@example.com, nope"}])
