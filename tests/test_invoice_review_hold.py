"""Exercise real workflow branching after a successful DataOps handoff."""
import copy
import json
from pathlib import Path

import pytest
import yaml

from src.dapier.engine.actions.code import run_code
from src.dapier.engine.actions.templating import render
from src.dapier.engine.logic import run_chain

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("workflow_id", ["invoice-intake", "invoice-body-completion"])
def test_embedded_code_tests_survive_workflow_publication(workflow_id):
    from src.dapier.engine.code_tests import run_code_tests
    from src.dapier.triggers.published_workflows import _scrub

    workflow = yaml.safe_load((ROOT / f"workflows/{workflow_id}.yaml").read_text())

    def check(actions):
        for action in actions:
            if action.get("tests"):
                report = run_code_tests(action)
                assert report["failed"] == 0, report
            for branch in ("then", "else"):
                check(action.get(branch) or [])

    # The published store removes null-valued fields, including fixtures.
    check(_scrub(workflow)["actions"])


@pytest.mark.parametrize("workflow_id", ["invoice-intake", "invoice-body-completion"])
@pytest.mark.parametrize("status", ["accepted", "duplicate"])
@pytest.mark.parametrize("company", [None, "", "   ", "Example Vendor Ltd"])
def test_company_controls_archive_or_visible_review_hold(workflow_id, status, company):
    event = json.loads((ROOT / "tests/fixtures/forwarded_invoice_no_company.json").read_text())
    if workflow_id == "invoice-body-completion":
        event = {
            "id": "synthetic-render-completion", "connector": "renderer",
            "event": "job.completed", "occurred_at": event["occurred_at"],
            "data": {"route": "invoice", "checksum": "a" * 64,
                     "source_event": event,
                     "output": {"bucket": "synthetic-rendered", "key": "body.pdf"}},
        }
    workflow = yaml.safe_load((ROOT / f"workflows/{workflow_id}.yaml").read_text())
    preview = {"invoiceId": "synthetic-review-id", "invoiceNumber": "SYNTHETIC-42"}
    if company is not None:
        preview["counterparty"] = company
    response = {"status": status, "intakeItemId": "synthetic-intake-id",
                "invoices": [preview]}
    outputs, archives, handoffs = {}, [], []

    def run_action(action, event, workflow_id, steps=None):
        if action["type"] == "code":
            return run_code(action, event, steps=steps)
        if action["type"] == "date_time":
            return {"formatted": "2026-10-24"}
        if action["type"] == "merge_pdfs":
            return {"filename": "synthetic-invoice.pdf",
                    "output": {"bucket": "synthetic-rendered", "key": "invoice.pdf"}}
        if action["type"] == "dataops":
            handoffs.append(copy.deepcopy(response))
            return copy.deepcopy(response)
        if action["type"] == "dropbox_upload":
            archives.append(render(action["filename"], event, steps))
            assert action["skip_existing"] is True
            assert action["strict_conflict"] is True
            return {"skipped": True}
        raise AssertionError(f"unexpected action {action['type']}")

    # Repeat the same envelope: no new review identity or archive filename.
    for _ in range(2):
        outputs.clear()
        assert run_chain(workflow_id, workflow["actions"], event, run_action,
                         step_outputs=outputs) is None
        holds = [entry["output"]["result"] for key, entry in outputs.items()
                 if key.endswith(".manual-review")]
        if company and company.strip():
            assert holds == []
        else:
            assert holds == [{"status": "held_for_review", "reason": "missing_company",
                              "review_system": "DataOps Finance",
                              "intakeItemId": "synthetic-intake-id",
                              "invoiceId": "synthetic-review-id"}]
            assert archives == []
    assert len(handoffs) == 2
    if company and company.strip():
        assert archives == ["2026-10-24-Example Vendor Ltd-SYNTHETIC-42-aaaaaaaaaaaa.pdf"] * 2


@pytest.mark.parametrize("workflow_id", ["invoice-intake", "invoice-body-completion"])
def test_dataops_transport_failure_still_raises(workflow_id):
    workflow = yaml.safe_load((ROOT / f"workflows/{workflow_id}.yaml").read_text())
    actions = workflow["actions"]
    if workflow_id == "invoice-intake":
        actions = actions[1]["then"]
    handoff = next(action for action in actions if action["type"] == "dataops")

    def unavailable(*args, **kwargs):
        raise RuntimeError("DataOps unavailable")

    with pytest.raises(RuntimeError, match="DataOps unavailable"):
        run_chain(workflow_id, [handoff], {"data": {}}, unavailable)
