"""Guard the shipped invoice-intake workflow file.

The canonical `workflows/invoice-intake.yaml` must satisfy the same
save-time validation the API enforces, so the file cannot rot away from
what a live workflow would accept.
"""
from pathlib import Path

import yaml

from src.dapier.connectors import registry


def test_the_shipped_invoice_intake_workflow_validates():
    workflow = yaml.safe_load((Path(__file__).resolve().parents[1] /
                               "workflows" / "invoice-intake.yaml").read_text())
    assert workflow["trigger"]["connector"] == "email"
    assert workflow["trigger"]["filters"]["route"]["equals"] == "invoice"
    registry.validate_action_chain(workflow["actions"])


def test_subject_cleanup_and_archive_identity():
    from src.dapier.engine.actions.code import run_code
    from src.dapier.engine.actions.templating import render

    workflow = yaml.safe_load((Path(__file__).resolve().parents[1] /
                               "workflows" / "invoice-intake.yaml").read_text())
    actions = workflow["actions"]
    assert [action["id"] for action in actions] == [
        "email-date", "clean-subject", "archive-attachment", "file-to-dataops",
    ]
    subjects = [
        "Fwd: Amazon Web Services Invoice Available [Account: 123] [Invoice ID: INV-1]",
        "Re: Fwd: Amazon web services Invoice Available [Account: 456] [Invoice ID: INV-2]",
        "Amazon Web Services",
    ]
    names = []
    for index, subject in enumerate(subjects):
        event = {"data": {"subject": subject, "attachments": [{"checksum": "sha256:" + str(index + 1) * 64}]}}
        output = run_code(actions[1], event)
        assert output["result"]["subject"] == "Amazon Web Services"
        assert event["data"]["subject"] == subject  # retain the original intake/audit data
        steps = {"email-date": {"output": {"formatted": "2026-10-03"}},
                 "clean-subject": {"output": output}}
        names.append(render(actions[2]["filename"], event, steps))
        assert run_code(actions[1], event)["result"] == output["result"]
    assert len(set(names)) == len(names)
    assert all(name.startswith("2026-10-03-Amazon Web Services-") for name in names)


def test_subject_cleanup_keeps_other_vendor_text_and_requires_document_identity():
    import pytest
    from src.dapier.engine.actions.code import run_code

    workflow = yaml.safe_load((Path(__file__).resolve().parents[1] /
                               "workflows" / "invoice-intake.yaml").read_text())
    action = next(action for action in workflow["actions"] if action["id"] == "clean-subject")
    event = {"data": {"subject": "Fwd: Stripe receipt", "attachments": [{"checksum": "a" * 64}]}}
    assert run_code(action, event)["result"]["subject"] == "Stripe receipt"
    with pytest.raises(RuntimeError, match="Attachment checksum is required"):
        run_code(action, {"data": {"subject": "Amazon Web Services"}})
