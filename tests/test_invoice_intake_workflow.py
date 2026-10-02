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
