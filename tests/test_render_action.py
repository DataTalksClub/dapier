"""render_html_to_pdf: input resolution (body object vs input_value template)
and the queued job shape."""

import json
from unittest.mock import MagicMock

import pytest

from src.dapier.engine.actions.render import run_render_job


EVENT = {
    "id": "email:abc",
    "connector": "email",
    "data": {
        "html": {"value": "<p>original</p>"},
        "subject": "Invoice",
    },
}

ACTION = {
    "id": "render-pdf",
    "type": "render_html_to_pdf",
    "input_field": "html",
    "output_key": "invoice-body/{event_id}.pdf",
    "output_bucket_env": "RENDER_ARTIFACTS_BUCKET",
}


@pytest.fixture
def queue_run(monkeypatch):
    """Run one render action with S3/SQS stubbed; returns the last put, the
    queued job, and the runner output."""
    import boto3

    puts, sends = [], []

    s3 = MagicMock()
    s3.put_object.side_effect = lambda **kwargs: puts.append(kwargs) or {}
    sqs = MagicMock()
    sqs.send_message.side_effect = lambda **kwargs: sends.append(kwargs) or {}

    monkeypatch.setattr(boto3, "client", lambda service: s3 if service == "s3" else sqs)
    monkeypatch.setenv("RENDER_ARTIFACTS_BUCKET", "render-bucket")
    monkeypatch.setenv("RENDER_QUEUE_URL", "https://sqs.test/render")

    def run(action, event, steps=None):
        assert sends == []
        output = run_render_job(action, event, "invoice-body-render", steps=steps)
        assert sends, "no render job was queued"
        return puts[0], json.loads(sends[0]["MessageBody"]), output

    return run


def test_body_object_value_is_the_source(queue_run):
    put, job, output = queue_run(ACTION, EVENT)
    assert put["Body"] == b"<p>original</p>"
    assert put["Bucket"] == "render-bucket"
    assert job["schema"] == "html-renderer.job.v1"
    assert job["input"]["bucket"] == "render-bucket"
    assert job["output"]["key"] == "invoice-body/email:abc.pdf"
    assert output["output"]["key"] == "invoice-body/email:abc.pdf"


def test_input_value_template_wins_over_input_field(queue_run):
    action = {**ACTION, "input_value": "{steps.clean.output.result.html}"}
    put, _, _ = queue_run(action, EVENT, steps={
        "clean": {"status": "completed", "output": {"result": {"html": "<p>clean</p>"}}}})
    assert put["Body"] == b"<p>clean</p>"


def test_input_value_reads_event_fields(queue_run):
    put, _, _ = queue_run({**ACTION, "input_value": "{html}"},
                          {"id": "e", "data": {"html": "<b>bold</b>"}})
    assert put["Body"] == b"<b>bold</b>"


def test_empty_rendered_input_value_fails(queue_run):
    with pytest.raises(ValueError, match="rendered empty"):
        queue_run({**ACTION, "input_value": "{steps.nope.output.result.html}"}, EVENT)


def test_missing_input_fails(queue_run):
    with pytest.raises(ValueError, match="email body object"):
        queue_run(ACTION, {"id": "e", "data": {}})
