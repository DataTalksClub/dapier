"""Guard the shipped invoice-intake workflow file.

The canonical `workflows/invoice-intake.yaml` must satisfy the same
save-time validation the API enforces, so the file cannot rot away from
what a live workflow would accept — and its code steps (triage,
subject cleanup, forwarded-header stripping) must behave as their tests
claim.
"""
from pathlib import Path

import yaml

from src.dapier.connectors import registry

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


def test_triage_routes_attachment_body_and_fails_on_two():
    from src.dapier.engine.actions.code import run_code

    triage = find_action(shipped_workflow(), "triage")
    assert triage is not None

    def run(attachments):
        return run_code(triage, {"data": {"attachments": attachments}})["result"]

    assert run([]) == {"has_attachment": False, "attachment_count": 0}
    assert run([{"filename": "invoice.pdf"}]) == {
        "has_attachment": True, "attachment_count": 1}

    import pytest

    with pytest.raises(RuntimeError, match="got 2 attachments"):
        run([{}, {}])


def test_subject_cleanup_and_archive_identity():
    from src.dapier.engine.actions.code import run_code
    from src.dapier.engine.actions.templating import render

    workflow = shipped_workflow()
    branch = next(action for action in workflow["actions"]
                  if action.get("id") == "route")["then"]
    assert [action["id"] for action in branch] == [
        "email-date", "clean-subject", "archive-attachment", "file-to-dataops",
    ]
    clean_subject, archive = branch[1], branch[2]
    subjects = [
        "Fwd: Amazon Web Services Invoice Available [Account: 123] [Invoice ID: INV-1]",
        "Re: Fwd: Amazon web services Invoice Available [Account: 456] [Invoice ID: INV-2]",
        "Amazon Web Services",
    ]
    names = []
    for index, subject in enumerate(subjects):
        event = {"data": {"subject": subject, "attachments": [{"checksum": "sha256:" + str(index + 1) * 64}]}}
        output = run_code(clean_subject, event)
        assert output["result"]["subject"] == "Amazon Web Services"
        assert event["data"]["subject"] == subject  # retain the original intake/audit data
        steps = {"email-date": {"output": {"formatted": "2026-10-03"}},
                 "clean-subject": {"output": output}}
        names.append(render(archive["filename"], event, steps))
        assert run_code(clean_subject, event)["result"] == output["result"]
    assert len(set(names)) == len(names)
    assert all(name.startswith("2026-10-03-Amazon Web Services-") for name in names)


def test_subject_cleanup_keeps_other_vendor_text_and_requires_document_identity():
    import pytest

    from src.dapier.engine.actions.code import run_code

    clean_subject = find_action(shipped_workflow(), "clean-subject")
    event = {"data": {"subject": "Fwd: Stripe receipt", "attachments": [{"checksum": "a" * 64}]}}
    assert run_code(clean_subject, event)["result"]["subject"] == "Stripe receipt"
    with pytest.raises(RuntimeError, match="Attachment checksum is required"):
        run_code(clean_subject, {"data": {"subject": "Amazon Web Services"}})


def test_body_only_branch_strips_and_renders():
    from src.dapier.engine.actions.code import run_code

    workflow = shipped_workflow()
    else_branch = next(action for action in workflow["actions"]
                       if action.get("id") == "route")["else"]
    assert [action["id"] for action in else_branch] == ["strip-forwarded", "render-pdf"]
    strip_forwarded, render_pdf = else_branch
    # The render consumes the stripped body through input_value.
    assert render_pdf["type"] == "render_html_to_pdf"
    assert render_pdf["input_value"] == "{steps.strip-forwarded.output.result.html}"

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
