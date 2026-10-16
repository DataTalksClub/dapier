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
    assert put["Body"].endswith(b"<p>original</p>")
    assert put["Bucket"] == "render-bucket"
    assert job["schema"] == "html-renderer.job.v1"
    assert job["input"]["bucket"] == "render-bucket"
    assert job["output"]["key"] == "invoice-body/email:abc.pdf"
    assert output["output"]["key"] == "invoice-body/email:abc.pdf"


def test_input_value_template_wins_over_input_field(queue_run):
    action = {**ACTION, "input_value": "{steps.clean.output.result.html}"}
    put, _, _ = queue_run(action, EVENT, steps={
        "clean": {"status": "completed", "output": {"result": {"html": "<p>clean</p>"}}}})
    assert put["Body"].endswith(b"<p>clean</p>")


def test_input_value_reads_event_fields(queue_run):
    put, _, _ = queue_run({**ACTION, "input_value": "{html}"},
                          {"id": "e", "data": {"html": "<b>bold</b>"}})
    assert put["Body"].endswith(b"<b>bold</b>")


def test_offline_receipt_keeps_styles_and_replaces_broken_logo(queue_run):
    html = ('<div style="font-family:verdana;font-size:14px">'
            '<a><img src="https://example.test/logo.png" width="162" '
            'alt="Google Play &amp; Partners"></a>'
            '<img src="cid:tracking" alt="">'
            '<table><tr><td>Total: 219,99 €</td></tr></table></div>')
    put, _, _ = queue_run({**ACTION, "input_value": "{html}"},
                          {"id": "receipt", "data": {"html": html}})
    document = put["Body"].decode()
    assert 'font-family:Arial,Helvetica,sans-serif' in document
    assert 'style="font-family:verdana,Arial,Helvetica,sans-serif;font-size:14px"' in document
    assert '<a>Google Play &amp; Partners</a>' in document
    assert '<img' not in document
    assert '<table><tr><td>Total: 219,99 €</td></tr></table>' in document


def test_document_styles_and_embedded_images_survive(queue_run):
    html = ('<!DOCTYPE html><html><head><style>body{font-family:Verdana}</style>'
            '</head><body><img src="data:image/png;base64,AAAA" width="20"/>'
            '<p>&#8364; &amp; tax</p></body></html>')
    put, _, _ = queue_run({**ACTION, "input_value": "{html}"},
                          {"id": "document", "data": {"html": html}})
    document = put["Body"].decode()
    assert document.startswith('<!DOCTYPE html><html><head><meta charset="utf-8">')
    assert document.index('body{font-family:Arial') < document.index('body{font-family:Verdana,Arial')
    assert 'src="data:image/png;base64,AAAA" width="20"/>' in document
    assert '<p>&#8364; &amp; tax</p>' in document


def test_font_fallback_keeps_embedded_font_definitions_and_generic_stacks():
    from src.dapier.engine.actions.render import _font_fallback

    css = ('@font-face{font-family:Receipt;src:url(data:font/woff2;base64,AAAA)}'
           '.title{font-family:"Receipt" !important}'
           'pre{font-family:monospace}')
    output = _font_fallback(css)
    assert '@font-face{font-family:Receipt;src:url(data:font/woff2;base64,AAAA)}' in output
    assert '.title{font-family:"Receipt",Arial,Helvetica,sans-serif !important}' in output
    assert 'pre{font-family:monospace}' in output


def test_empty_rendered_input_value_fails(queue_run):
    with pytest.raises(ValueError, match="rendered empty"):
        queue_run({**ACTION, "input_value": "{steps.nope.output.result.html}"}, EVENT)


def test_missing_input_fails(queue_run):
    with pytest.raises(ValueError, match="email body object"):
        queue_run(ACTION, {"id": "e", "data": {}})
