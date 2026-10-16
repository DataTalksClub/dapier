"""Guard the shipped invoice-intake workflow file.

The canonical `workflows/invoice-intake.yaml` must satisfy the same
save-time validation the API enforces, so the file cannot rot away from
what a live workflow would accept — and its code steps (triage, subject
cleanup, forwarded-header stripping) must behave as their tests claim.
"""
from pathlib import Path

import yaml

WORKFLOW_FILE = Path(__file__).resolve().parents[1] / "workflows" / "invoice-intake.yaml"


def shipped_workflow():
    return yaml.safe_load(WORKFLOW_FILE.read_text())


def find_action(workflow, action_id):
    """A step anywhere in the chain, including condition branches."""
    for action in workflow["actions"]:
        if action.get("id") == action_id:
            return action
        for branch in ("then", "else"):
            found = find_action({"actions": action.get(branch) or []}, action_id)
            if found:
                return found
    return None


def test_the_shipped_invoice_intake_workflow_validates():
    from src.dapier.api.designer_store.validation import parse_workflow

    workflow = parse_workflow(WORKFLOW_FILE.read_text())
    assert workflow["trigger"]["connector"] == "email"
    assert workflow["trigger"]["filters"]["route"]["equals"] == "invoice"


def test_triage_drops_non_pdfs_and_picks_or_merges():
    from src.dapier.engine.actions.code import run_code

    triage = find_action(shipped_workflow(), "triage")
    assert triage is not None

    def run(attachments):
        return run_code(triage, {"data": {"attachments": attachments}})["result"]

    pdf = {"content_type": "application/pdf", "checksum": "sha256:" + "a" * 64}

    body = run([])
    assert body["has_attachment"] is False
    assert body["mode"] == "body"
    assert body["document_plan"] == {"mode": "body", "attachments": []}

    single = run([dict(pdf, filename="invoice.pdf")])
    assert single["mode"] == "single"
    assert single["document_key"] == "a" * 12
    assert single["document_plan"]["attachments"] == [dict(pdf, filename="invoice.pdf")]

    # Stripe sends the invoice and the receipt; the receipt is the keeper.
    pair = run([dict(pdf, filename="Invoice-4T7.pdf", checksum="sha256:" + "b" * 64),
                dict(pdf, filename="Receipt-4T7.pdf", checksum="sha256:" + "c" * 64)])
    assert pair["mode"] == "receipt"
    assert pair["document_plan"]["attachments"] == [
        dict(pdf, filename="Receipt-4T7.pdf", checksum="sha256:" + "c" * 64)]
    assert pair["document_key"] == "c" * 12

    # Ambiguity merges instead of picking: no receipt name, or several.
    merged = run([dict(pdf, filename="a.pdf"),
                  dict(pdf, filename="b.pdf", checksum="sha256:" + "b" * 64)])
    assert merged["mode"] == "merge"
    assert [a["filename"] for a in merged["document_plan"]["attachments"]] == ["a.pdf", "b.pdf"]
    assert merged["document_key"] == "6173cf9d3267"
    two_receipts = run([dict(pdf, filename="Receipt-1.pdf"),
                        dict(pdf, filename="Receipt-2.pdf", checksum="sha256:" + "b" * 64)])
    assert two_receipts["mode"] == "merge"

    # Non-PDF attachments never make the document.
    mixed = run([{"content_type": "text/html; charset=utf-8", "filename": "Terms_of_Service.html"},
                 {"content_type": "image/png", "filename": "scan.png"},
                 dict(pdf, filename="invoice.pdf")])
    assert mixed["attachment_count"] == 3
    assert mixed["document_count"] == 1
    assert mixed["document_plan"]["attachments"] == [dict(pdf, filename="invoice.pdf")]

    import pytest

    with pytest.raises(RuntimeError, match="checksum is required"):
        run([{"content_type": "application/pdf", "filename": "invoice.pdf"}])


def test_attachment_branch_builds_and_archives_one_document():
    workflow = shipped_workflow()
    branch = next(action for action in workflow["actions"]
                  if action.get("id") == "route")["then"]
    assert [action["id"] for action in branch] == [
        "email-date", "clean-subject", "make-document", "file-to-dataops",
        "build-filename", "archive-document",
    ]
    make_document, intake, build_filename, archive = branch[2], branch[3], branch[4], branch[5]
    assert make_document["type"] == "merge_pdfs"
    assert make_document["plan_step"] == "triage"
    # DataOps runs before the archive so its invoice preview can name the
    # file. The archive requires the company preview; it must never silently
    # fall back to an email subject that looks like a successful filing.
    assert intake["document_step"] == "route.then.make-document"
    assert intake["connection_id"] == "dataops"
    assert "steps.route.then.build-filename" in archive["filename"]
    assert archive["source"] == "step"
    assert archive["step"] == "route.then.make-document"
    assert archive.get("skip_existing") is True

    from src.dapier.engine.code_tests import run_code_tests

    report = run_code_tests(build_filename)
    assert report["failed"] == 0, report


def test_subject_cleanup_still_normalizes_vendors():
    from src.dapier.engine.actions.code import run_code

    clean_subject = find_action(shipped_workflow(), "clean-subject")
    event = {"data": {"subject": "Fwd: Stripe receipt [Invoice ID: INV-1]"}}
    assert run_code(clean_subject, event)["result"]["subject"] == "Stripe receipt"
    event = {"data": {"subject": "Re: Amazon Web Services Invoice Available [Account: 123]"}}
    assert run_code(clean_subject, event)["result"]["subject"] == "Amazon Web Services"
    event = {"data": {"subject": ""}}
    assert run_code(clean_subject, event)["result"]["subject"] == "Invoice"


def test_body_only_branch_strips_and_renders():
    from src.dapier.engine.actions.code import run_code

    workflow = shipped_workflow()
    else_branch = next(action for action in workflow["actions"]
                       if action.get("id") == "route")["else"]
    assert [action["id"] for action in else_branch] == ["strip-forwarded", "render-pdf"]
    strip_forwarded, render_pdf = else_branch
    # The render consumes the stripped body through input_value; branch
    # steps are recorded under their prefixed run-history id.
    assert render_pdf["type"] == "render_html_to_pdf"
    assert render_pdf["input_value"] == "{steps.route.else.strip-forwarded.output.result.html}"

    forwarded = ("<div>---------- Forwarded message ----------</div>"
                 "<div>From: Google Play &lt;googleplay-noreply@google.com&gt;</div>"
                 "<div>Date: Fri, Oct 2, 2026, 08:56</div>"
                 "<div>Subject: Your Google Play Order Receipt</div>"
                 "<div>To: &lt;alexey.s.grigoriev@gmail.com&gt;</div>"
                 "<div><table><tr><td>Order total: 0.99 EUR</td></tr></table></div>")
    event = {"data": {"body": {"html": {"value": forwarded}}}}
    result = run_code(strip_forwarded, event)["result"]
    assert result["forwarded_header_removed"] is True
    assert "Forwarded message" not in result["html"]
    assert "googleplay-noreply" not in result["html"]
    assert "Order total: 0.99 EUR" in result["html"]


def test_missing_body_never_turns_into_a_blank_pdf():
    import pytest
    from src.dapier.engine.actions.code import run_code

    strip_forwarded = find_action(shipped_workflow(), "strip-forwarded")
    for body in ({}, {"html": None, "text": {"value": "  "}}):
        with pytest.raises(RuntimeError, match="refusing a blank PDF"):
            run_code(strip_forwarded, {"data": {"body": body}})
