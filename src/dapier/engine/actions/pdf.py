"""merge_pdfs action: turn a code step's document plan into one PDF.

The plan is the ``plan_step`` result's ``document_plan`` and names the
attachments that make up the run's one document:

- a single attachment passes through untouched — the output references the
  stored object, so checksums and bytes stay the original's;
- several attachments merge into one PDF (pages in plan order) stored under
  ``output_key`` in the artifacts bucket.

Which attachments belong in the plan — dropping non-PDF parts, keeping a
receipt over a paired invoice — is the workflow's triage step's policy; this
action is only the mechanical "exactly one document out" half.
"""
import io
import os


def run_merge_pdfs(action, event, workflow_id, steps=None):
    plan = _plan(action, steps)
    attachments = [a for a in (plan.get("attachments") or [])
                   if isinstance(a, dict)]
    if not attachments:
        raise ValueError("merge_pdfs plan carries no attachments")
    refs = []
    for attachment in attachments:
        ref = attachment.get("s3") or {}
        if not (isinstance(ref, dict) and ref.get("bucket") and ref.get("key")):
            raise ValueError("merge_pdfs plan attachment has no s3 reference")
        refs.append((attachment, ref))
    if len(refs) == 1:
        attachment, ref = refs[0]
        return {"merged": False, "parts": 1, "filename": _filename(attachment),
                "output": {"bucket": ref["bucket"], "key": ref["key"]}}
    from . import base

    merged, pages = _merge([base._s3_body(ref) for _, ref in refs])
    bucket = (action.get("output_bucket")
              or os.environ[action.get("output_bucket_env")
                            or "RENDER_ARTIFACTS_BUCKET"])
    event_id = str(event.get("id") or "").replace("/", "_")
    key = str(action.get("output_key") or "merged/{event_id}.pdf").format(event_id=event_id)
    import boto3

    boto3.client("s3").put_object(Bucket=bucket, Key=key, Body=merged,
                                  ContentType="application/pdf",
                                  ServerSideEncryption="AES256")
    return {"merged": True, "parts": len(refs), "pages": pages,
            "filename": "merged.pdf",
            "output": {"bucket": bucket, "key": key}}


def _plan(action, steps):
    """The plan step's ``result.document_plan`` (the same shape a condition's
    ``steps.<id>.output.result.*`` reference reads)."""
    ref = str(action.get("plan_step") or "").strip()
    if not ref:
        raise ValueError("merge_pdfs needs plan_step (a code step that outputs document_plan)")
    node = (steps or {}).get(ref)
    if not isinstance(node, dict):
        raise ValueError(f"merge_pdfs plan_step '{ref}' has no recorded output")
    output = node.get("output") if isinstance(node.get("output"), dict) else {}
    result = output.get("result") if isinstance(output.get("result"), dict) else {}
    plan = result.get("document_plan")
    if not isinstance(plan, dict):
        raise ValueError(f"merge_pdfs plan_step '{ref}' result carries no document_plan")
    return plan


def _filename(attachment):
    from . import base

    return base._safe_filename(attachment.get("filename") or "document.pdf")


def _merge(bodies):
    """Concatenate PDFs page-wise, in order. Returns (bytes, page_count)."""
    from pypdf import PdfReader, PdfWriter

    writer = PdfWriter()
    pages = 0
    for body in bodies:
        for page in PdfReader(io.BytesIO(body)).pages:
            writer.add_page(page)
            pages += 1
    buffer = io.BytesIO()
    writer.write(buffer)
    return buffer.getvalue(), pages
