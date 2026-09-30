"""Invoice cutover preserves attachment and render-output selection."""
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml

from src.dapier import engine
from src.dapier.engine.actions.dropbox import _upload_files

ROOT = Path(__file__).resolve().parents[1] / "migrations/multi-trigger-workflows"


@pytest.mark.parametrize("connector,event_type,data,source", [
    ("email", "message.received", {"route": "invoice-attachment", "attachments": [{"filename": "invoice.pdf", "s3": {"bucket": "test", "key": "invoice.pdf"}}]}, "attachment"),
    ("renderer", "job.completed", {"output": {"bucket": "test", "key": "rendered/invoice.pdf"}}, "output"),
])
def test_invoice_inputs_upload_exactly_once(connector, event_type, data, source):
    workflow = yaml.safe_load((ROOT / "invoice-filing.yaml").read_text())
    calls = []
    def capture(action, envelope, workflow_id, steps=None):
        calls.append(action)
        files = _upload_files(action, envelope["data"])
        assert len(files) == 1
        assert files[0]["filename"] == "invoice.pdf"
        return {"uploaded": ["/_dtc_paperwork/income-invoices/invoice.pdf"]}
    with patch.object(engine, "_run_connector", capture):
        assert engine.execute({"connector": connector, "event": event_type, "data": data}, workflows=[workflow]) == ["invoice-filing"]
    assert len(calls) == 1
    assert calls[0]["type"] == "dropbox_upload"
    assert calls[0]["source"] == source
    assert calls[0]["folder"] == "_dtc_paperwork/income-invoices"
