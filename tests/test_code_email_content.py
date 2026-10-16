"""Stored email bodies must reach transforms before document rendering."""
from copy import deepcopy
from io import BytesIO
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml

from src.dapier.engine.actions.code import run_code, run_js


@pytest.mark.parametrize("runner,source", [
    (run_code, 'input["body"]["html"]["value"]'),
    (run_js, 'return input.body.html.value;'),
])
def test_stored_body_reaches_both_languages_without_changing_event(runner, source):
    event = {"connector": "email", "data": {"body": {"html": {
        "s3": {"bucket": "mail", "key": "body.html"}}}}}
    original = deepcopy(event)
    with patch("boto3.client") as client:
        client.return_value.get_object.return_value = {"Body": BytesIO(b"<p>Refund: 877.39 EUR</p>")}
        result = runner({"code": source}, event)
    assert result["result"] == "<p>Refund: 877.39 EUR</p>"
    assert event == original


def test_invoice_transform_keeps_refunds_from_referenced_body():
    flow = yaml.safe_load((Path(__file__).parents[1] / "workflows/invoice-intake.yaml").read_text())
    action = next(a for a in flow["actions"][1]["else"] if a["id"] == "strip-forwarded")
    event = {"connector": "email", "data": {"body": {"html": {
        "s3": {"bucket": "mail", "key": "body.html"}}}}}
    html = b"---------- Forwarded message ----------\nFrom: X Subscription\nSubject: Refund\n\nRefunds: 877.39 EUR and 7.64 EUR"
    with patch("boto3.client") as client:
        client.return_value.get_object.return_value = {"Body": BytesIO(html)}
        result = run_code(action, event)["result"]
    assert result["forwarded_header_removed"]
    assert "877.39 EUR and 7.64 EUR" in result["html"]


@pytest.mark.parametrize("runner", [run_code, run_js])
def test_unreadable_body_fails_instead_of_producing_empty_document(runner):
    event = {"connector": "email", "data": {"body": {"html": {
        "s3": {"bucket": "mail", "key": "missing"}}}}}
    with patch("boto3.client") as client:
        client.return_value.get_object.side_effect = RuntimeError("private storage details")
        with pytest.raises(ValueError, match="email body could not be loaded"):
            runner({"code": "1"}, event)
