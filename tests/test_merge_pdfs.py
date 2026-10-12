"""merge_pdfs: exactly one document out of a code step's attachment plan."""
import io
import unittest.mock as mock

import boto3
import pytest
from moto import mock_aws
from pypdf import PdfReader, PdfWriter

from src.dapier.engine.actions import base
from src.dapier.engine.actions.pdf import run_merge_pdfs

import src.dapier.connectors  # noqa: F401  (import = registration)


def pdf_page(width, height):
    writer = PdfWriter()
    writer.add_blank_page(width=width, height=height)
    buffer = io.BytesIO()
    writer.write(buffer)
    return buffer.getvalue()


def plan_step(attachments):
    return {"triage": {"output": {
        "result": {"document_plan": {"mode": "merge", "attachments": attachments}}}}}


def attachment(index):
    return {"filename": f"p{index}.pdf",
            "s3": {"bucket": "mail", "key": f"msg/p{index}.pdf"}}


ACTION = {"plan_step": "triage", "output_key": "invoice-document/{event_id}.pdf"}


def test_a_single_attachment_passes_through_without_a_copy():
    output = run_merge_pdfs(ACTION, {"id": "ev1"}, "invoice-intake",
                            steps=plan_step([dict(attachment(0), checksum="sha256:" + "a" * 64)]))
    assert output == {"merged": False, "parts": 1, "filename": "p0.pdf",
                      "output": {"bucket": "mail", "key": "msg/p0.pdf"}}


def test_a_nameless_attachment_passes_through_as_document_pdf():
    output = run_merge_pdfs(ACTION, {"id": "ev1"}, "invoice-intake",
                            steps=plan_step([{"s3": {"bucket": "mail", "key": "msg/x"}}]))
    assert output["filename"] == "document.pdf"


@mock_aws
def test_several_attachments_merge_page_wise_in_plan_order():
    client = boto3.client("s3", region_name="eu-west-1")
    client.create_bucket(Bucket="artifacts",
                         CreateBucketConfiguration={"LocationConstraint": "eu-west-1"})
    bodies = [pdf_page(100, 100), pdf_page(200, 300), pdf_page(150, 150)]
    mapping = {f"msg/p{i}.pdf": bodies[i] for i in range(3)}
    with mock.patch.object(base, "_s3_body", side_effect=lambda ref: mapping[ref["key"]]), \
         mock.patch.dict("os.environ", {"RENDER_ARTIFACTS_BUCKET": "artifacts"}):
        output = run_merge_pdfs(ACTION, {"id": "ev/1"}, "invoice-intake",
                                steps=plan_step([attachment(i) for i in range(3)]))
    assert output["merged"] is True
    assert output["parts"] == 3
    assert output["pages"] == 3
    assert output["filename"] == "merged.pdf"
    assert output["output"] == {"bucket": "artifacts", "key": "invoice-document/ev_1.pdf"}
    stored = client.get_object(Bucket="artifacts", Key="invoice-document/ev_1.pdf")
    assert stored["ContentType"] == "application/pdf"
    pages = PdfReader(io.BytesIO(stored["Body"].read())).pages
    assert [int(page.mediabox.width) for page in pages] == [100, 200, 150]


def test_a_bad_plan_fails_loudly():
    with pytest.raises(ValueError, match="no recorded output"):
        run_merge_pdfs(ACTION, {"id": "e"}, "wf", steps={})
    with pytest.raises(ValueError, match="carries no attachments"):
        run_merge_pdfs(ACTION, {"id": "e"}, "wf", steps=plan_step([]))
    with pytest.raises(ValueError, match="no s3 reference"):
        run_merge_pdfs(ACTION, {"id": "e"}, "wf", steps=plan_step([{"filename": "a.pdf"}]))
    with pytest.raises(ValueError, match="needs plan_step"):
        run_merge_pdfs({"output_key": "x"}, {"id": "e"}, "wf", steps=plan_step([attachment(0)]))


def test_the_registry_lists_merge_pdfs():
    from src.dapier.connectors import registry

    spec = registry.ACTIONS["merge_pdfs"]
    assert spec.required == frozenset({"plan_step", "output_key"})
    assert spec.optional == frozenset({"output_bucket", "output_bucket_env"})
