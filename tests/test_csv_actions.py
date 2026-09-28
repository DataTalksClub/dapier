"""The csv_parse / csv_format actions: inline and staged-S3 parsing, header
and no-header rows, delimiter handling, the format round-trip, and the
registry entries' field contracts. The staged file is moto's, where the
runner resolves it through boto3 like every S3-reading action."""
import json

import boto3
import pytest
from moto import mock_aws

from src.dapier.connectors import registry
from src.dapier.engine.actions import csv

EVENT = {
    "id": "evt-1",
    "connector": "email",
    "event": "message.received",
    "data": {"sep": ";"},
}

STEPS = {"read": {"status": "completed",
                  "output": {"s3": {"bucket": "staged", "key": "in/rows.csv"}}}}

CSV_TEXT = "name,amount\nAcme,180.00\nGlobex,92.50\n"


def staged_file(body):
    """Put ``body`` in moto's staged bucket (eu-west-1 wants its constraint)."""
    client = boto3.client("s3", region_name="eu-west-1")
    client.create_bucket(
        Bucket="staged", CreateBucketConfiguration={"LocationConstraint": "eu-west-1"})
    client.put_object(Bucket="staged", Key="in/rows.csv", Body=body)


# --- csv_parse: source selection -------------------------------------------------


def test_parse_inline_content_into_dict_rows():
    output = csv.run_csv_parse({"content": CSV_TEXT}, EVENT)

    assert output == {
        "headers": ["name", "amount"],
        "rows": [{"name": "Acme", "amount": "180.00"},
                 {"name": "Globex", "amount": "92.50"}],
        "count": 2,
    }


def test_parse_values_stay_strings():
    output = csv.run_csv_parse({"content": "id,ok\n007,false\n"}, EVENT)

    assert output["rows"] == [{"id": "007", "ok": "false"}]


def test_parse_with_a_staged_s3_source():
    with mock_aws():
        staged_file(CSV_TEXT.encode())

        output = csv.run_csv_parse(
            {"source_s3": {"bucket": "{steps.read.output.s3.bucket}",
                           "key": "{steps.read.output.s3.key}"}},
            EVENT, steps=STEPS)

    assert output["count"] == 2
    assert output["rows"][0]["name"] == "Acme"


def test_parse_source_s3_swallows_a_bom():
    with mock_aws():
        staged_file("\ufeffname,amount\nAcme,1\n".encode())

        output = csv.run_csv_parse(
            {"source_s3": {"bucket": "staged", "key": "in/rows.csv"}}, EVENT)

    assert output["headers"] == ["name", "amount"]


def test_parse_takes_exactly_one_source():
    with pytest.raises(ValueError, match="not both"):
        csv.run_csv_parse({"content": CSV_TEXT,
                           "source_s3": {"bucket": "staged", "key": "rows.csv"}}, EVENT)

    with pytest.raises(ValueError, match="content or source_s3"):
        csv.run_csv_parse({}, EVENT)

    with pytest.raises(ValueError, match="content or source_s3"):
        csv.run_csv_parse({"source_s3": {"bucket": "staged"}}, EVENT)


# --- csv_parse: dialect and header handling --------------------------------------


def test_parse_honors_a_delimiter_field():
    output = csv.run_csv_parse(
        {"content": "name;amount\nAcme;180.00\n", "delimiter": ";"}, EVENT)

    assert output["rows"] == [{"name": "Acme", "amount": "180.00"}]


def test_parse_delimiter_takes_templates():
    output = csv.run_csv_parse(
        {"content": "name;amount\nAcme;180.00\n", "delimiter": "{sep}"}, EVENT)

    assert output["rows"] == [{"name": "Acme", "amount": "180.00"}]


def test_parse_without_a_header_row_lists_rows():
    output = csv.run_csv_parse(
        {"content": "Acme,180.00\nGlobex,92.50\n", "header_row": False}, EVENT)

    assert output == {"headers": [], "rows": [["Acme", "180.00"], ["Globex", "92.50"]],
                      "count": 2}


def test_parse_header_row_false_accepts_string_literals():
    for literal in (False, "false", "no", "0"):
        output = csv.run_csv_parse(
            {"content": "Acme,1\n", "header_row": literal}, EVENT)
        assert output["rows"] == [["Acme", "1"]], literal


def test_parse_header_cells_are_stripped_and_short_rows_padded():
    output = csv.run_csv_parse({"content": " name , amount \nAcme\n"}, EVENT)

    assert output["rows"] == [{"name": "Acme", "amount": ""}]


def test_parse_blank_lines_yield_no_rows():
    output = csv.run_csv_parse({"content": "\n\n"}, EVENT)

    assert output == {"headers": [], "rows": [], "count": 0}


# --- csv_format -------------------------------------------------------------------


def test_format_dict_rows_write_the_header_row_by_default():
    output = csv.run_csv_format(
        {"rows": [{"name": "Acme", "amount": "180.00"}]}, EVENT)

    assert output == {"csv": "name,amount\r\nAcme,180.00\r\n", "count": 1}


def test_format_round_trips_a_parse():
    parsed = csv.run_csv_parse({"content": CSV_TEXT}, EVENT)
    formatted = csv.run_csv_format(
        {"rows": parsed["rows"], "headers": parsed["headers"]}, EVENT)
    reparsed = csv.run_csv_parse({"content": formatted["csv"]}, EVENT)

    assert reparsed == parsed


def test_format_explicit_headers_order_and_project_the_columns():
    output = csv.run_csv_format(
        {"rows": [{"name": "Acme", "amount": "180.00"}], "headers": ["amount", "name"]},
        EVENT)

    assert output["csv"] == "amount,name\r\n180.00,Acme\r\n"


def test_format_defaults_headers_to_the_first_rows_keys():
    output = csv.run_csv_format(
        {"rows": [{"b": "2", "a": "1"}, {"b": "4", "a": "3"}]}, EVENT)

    assert output["csv"] == "b,a\r\n2,1\r\n4,3\r\n"


def test_format_list_rows_without_headers_write_no_header_row():
    output = csv.run_csv_format({"rows": [["Acme", "180.00"]]}, EVENT)

    assert output["csv"] == "Acme,180.00\r\n"


def test_format_cells_take_templates():
    output = csv.run_csv_format(
        {"rows": [{"name": "{subject}", "amount": "{amount}"}]},
        {"data": {"subject": "Invoice", "amount": "42"}})

    assert output["csv"] == "name,amount\r\nInvoice,42\r\n"


def test_format_accepts_rows_and_headers_as_json_strings():
    output = csv.run_csv_format(
        {"rows": json.dumps([{"name": "Acme"}]), "headers": json.dumps(["name"])},
        EVENT)

    assert output["csv"] == "name\r\nAcme\r\n"


def test_format_honors_the_delimiter():
    output = csv.run_csv_format(
        {"rows": [{"name": "Acme"}], "delimiter": ";"}, EVENT)

    assert output["csv"] == "name\r\nAcme\r\n"


def test_format_rejects_rows_that_are_not_a_list():
    with pytest.raises(ValueError, match="rows must be"):
        csv.run_csv_format({"rows": {"name": "Acme"}}, EVENT)

    with pytest.raises(ValueError, match="rows must be"):
        csv.run_csv_format({"rows": "not json"}, EVENT)

    with pytest.raises(ValueError, match="rows must be"):
        csv.run_csv_format({"rows": ["a string is not a row"]}, EVENT)


# --- the registry entries ---------------------------------------------------------


def test_the_actions_register_with_their_field_contracts():
    parse = registry.ACTIONS["csv_parse"]
    fmt = registry.ACTIONS["csv_format"]

    assert parse.required == frozenset()
    assert parse.optional == frozenset({"content", "source_s3", "delimiter", "header_row"})
    assert fmt.required == frozenset({"rows"})
    assert fmt.optional == frozenset({"headers", "delimiter"})
    keys = {field["key"] for field in parse.fields} | {field["key"] for field in fmt.fields}
    assert keys == {"content", "source_s3", "delimiter", "header_row", "rows", "headers"}


def test_a_save_with_no_source_fails_validation_only_at_run_time():
    # Exactly-one-of is a run-time rule: nothing is required at save time,
    # mirroring slack_upload_file's source trio.
    from src.dapier.connectors.registry import validate_action_chain

    validate_action_chain([{"type": "csv_parse", "delimiter": ","}])  # must not raise


def test_the_engine_dispatches_through_the_registry():
    from src.dapier.connectors.registry import run_action

    output = run_action({"type": "csv_parse", "content": "a\n1\n"}, {"data": {}}, "wf-1")

    assert output == {"headers": ["a"], "rows": [{"a": "1"}], "count": 1}


if __name__ == "__main__":
    pytest.main([__file__])
