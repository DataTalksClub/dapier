"""invoice_parse and bookkeeping_stage: attachment pickup, both extraction
tiers, and the pending write. The deterministic tier parses a sanitized
real-invoice fixture; the AI tier rides an injected transport (no network,
test_ai_action's seam); the store is a fake table."""
import json
import os
import unittest.mock as mock
from pathlib import Path

import pytest

from src.dapier import bookkeeping_store
from src.dapier.connectors import registry
from src.dapier.connectors.registry import ActionError, validate_action_chain
from src.dapier.engine.actions import bookkeeping, invoice
from src.dapier.engine.actions.webhook import HttpError
from tests.test_bookkeeping_store import FakeBookkeepingTable
from tests.test_bookkeeping_store import FakeBookkeepingTable

FIXTURES = Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "invoices"

LLM_ENV = {
    "COPILOT_LLM_API_KEY": "test-key",
    "COPILOT_LLM_BASE_URL": "https://llm.example.test/v1",
    "COPILOT_LLM_MODEL": "test-model",
}

EVENT = {
    "id": "email:evt-1",
    "connector": "email",
    "event": "message.received",
    "data": {
        "route": "invoice",
        "message_id": "<m-1@dtc>",
        "subject": "Your AWS invoice",
        "sender": {"header": "aws-receivables@example.test"},
        "date": "Fri, 2 Oct 2026 07:00:00 +0000",
        "attachments": [
            {"filename": "notes.txt", "content_type": "text/plain",
             "s3": {"bucket": "inbound", "key": "m-1/notes.txt"}},
            {"filename": "invoice.pdf", "content_type": "application/pdf",
             "checksum": "sha256:abc",
             "s3": {"bucket": "inbound", "key": "m-1/invoice.pdf"}},
        ],
    },
}


def fixture_text(name):
    return (FIXTURES / f"{name}.txt").read_text()


def test_chain_validates_like_any_connector_action():
    chain = [{"type": "invoice_parse", "ai_fallback": True},
             {"type": "bookkeeping_stage"}]
    validate_action_chain(chain)
    with pytest.raises(ActionError):
        validate_action_chain([{"type": "invoice_parse", "prompt": "nope"}])
    with pytest.raises(ActionError):
        validate_action_chain([{"type": "bookkeeping_stage", "entry": "x"}])


def test_pick_attachment_prefers_the_pdf():
    chosen = invoice.pick_attachment(EVENT)
    assert chosen["filename"] == "invoice.pdf"
    by_index = invoice.pick_attachment(EVENT, 0)
    assert by_index["filename"] == "notes.txt"


def test_pick_attachment_is_loud_about_misses():
    empty = {"data": {"attachments": []}}
    with pytest.raises(ValueError):
        invoice.pick_attachment(empty)
    inline = {"data": {"attachments": [{"filename": "a.pdf"}]}}
    with pytest.raises(ValueError):
        invoice.pick_attachment(inline)


def test_deterministic_parse_of_a_real_aws_invoice():
    with mock.patch.object(invoice.base, "_s3_body", return_value=b"pdf-bytes"), \
            mock.patch.object(invoice, "pdf_text",
                              return_value=fixture_text("20261002-081535-01-EUINDE26-2122792")):
        output = registry.run_action({"type": "invoice_parse"}, EVENT, "wf-1")
    assert output["ok"] is True
    assert output["matched"] is True
    assert output["source"] == "deterministic"
    assert output["entry"]["invoice_number"] == "EUINDE26-2122792"
    assert output["entry"]["amount"] == 70.57
    assert output["pdf"]["s3"] == {"bucket": "inbound", "key": "m-1/invoice.pdf"}


def test_unknown_vendor_without_fallback_matches_nothing():
    with mock.patch.object(invoice.base, "_s3_body", return_value=b"pdf-bytes"), \
            mock.patch.object(invoice, "pdf_text",
                              return_value=fixture_text("dropbox_hetzner-2026-07")):
        output = registry.run_action({"type": "invoice_parse"}, EVENT, "wf-1")
    assert output == {"ok": True, "matched": False, "source": None,
                      "pdf": output["pdf"], "text_chars": output["text_chars"]}


class AiTransport:
    """One canned chat-completion reply, the webhook seam's shape."""

    def __init__(self, content):
        self.content = content
        self.calls = []

    def __call__(self, method, url, *, headers=None, body=None, timeout=15):
        self.calls.append({"method": method, "url": url, "body": json.loads(body)})
        reply = {"choices": [{"message": {"content": self.content}}]}
        return 200, json.dumps(reply).encode()


def run_ai_fallback(content):
    transport = AiTransport(content)
    with mock.patch.dict(os.environ, LLM_ENV), \
            mock.patch.object(invoice.base, "_s3_body", return_value=b"pdf-bytes"), \
            mock.patch.object(invoice, "pdf_text",
                              return_value=fixture_text("dropbox_hetzner-2026-07")):
        output = invoice.run_invoice_parse(
            {"type": "invoice_parse", "ai_fallback": True}, EVENT, "wf-1",
            transport=transport)
    return output, transport


def test_ai_fallback_extracts_unknown_vendors():
    reply = json.dumps({
        "provider": "Hetzner", "what": "Dedicated Server", "invoice_number": "1001",
        "date_issued": "2026-07-01", "date_paid": None, "amount": 45.70,
        "currency": "EUR", "vat": 7.30, "period": "06/2026", "account": "DataTalks.Club",
    })
    output, transport = run_ai_fallback(reply)
    assert output["matched"] is True
    assert output["source"] == "ai"
    assert output["entry"]["provider"] == "Hetzner"
    assert output["entry"]["amount"] == 45.70
    assert output["entry"]["currency"] == "EUR"
    assert output["model"] == "test-model"
    # The prompt carried the invoice text, braces escaped, and asked for JSON.
    prompt = transport.calls[0]["body"]["messages"][-1]["content"]
    assert "Hetzner Online GmbH" in prompt
    assert transport.calls[0]["body"]["response_format"] == {"type": "json_object"}


def test_ai_reply_missing_required_fields_matches_nothing():
    output, _ = run_ai_fallback(json.dumps({"note": "not an invoice"}))
    assert output["matched"] is False
    assert "provider or amount" in output["ai_error"]


def test_ai_reply_that_will_not_parse_is_a_verdict():
    output, _ = run_ai_fallback("I could not read this invoice, sorry.")
    assert output["matched"] is False
    assert output["ai_error"]


def test_ai_transport_failure_raises_like_any_http_step():
    def transport(method, url, *, headers=None, body=None, timeout=15):
        raise HttpError("ai_complete returned HTTP 500", status=500)

    with mock.patch.dict(os.environ, LLM_ENV), \
            mock.patch.object(invoice.base, "_s3_body", return_value=b"pdf-bytes"), \
            mock.patch.object(invoice, "pdf_text",
                              return_value=fixture_text("dropbox_hetzner-2026-07")), \
            pytest.raises(HttpError):
        invoice.run_invoice_parse({"type": "invoice_parse", "ai_fallback": True},
                                  EVENT, "wf-1", transport=transport)


def test_stage_files_the_parse_output_for_review():
    store = bookkeeping_store  # the fake table rides the store's _table seam
    parsed = {"ok": True, "matched": True, "source": "deterministic",
              "entry": {"provider": "AWS", "invoice_number": "EUINDE26-1",
                        "amount": 70.57, "currency": "USD"},
              "pdf": {"filename": "invoice.pdf",
                      "s3": {"bucket": "inbound", "key": "m-1/invoice.pdf"}}}
    fake = store._table
    with mock.patch.object(store, "_table", return_value=FakeBookkeepingTable()):
        output = bookkeeping.run_bookkeeping_stage(
            {"type": "bookkeeping_stage", "entry_from": "parse"}, EVENT, "invoice-intake",
            steps={"parse": {"status": "completed", "output": parsed}})
        queued = store.list_entries()
    assert output["status"] == "pending"
    assert output["provider"] == "AWS"
    assert len(queued) == 1
    entry = queued[0]
    assert entry["entry"]["invoice_number"] == "EUINDE26-1"
    assert entry["source"] == "deterministic"
    assert entry["workflow_id"] == "invoice-intake"
    assert entry["message"]["subject"] == "Your AWS invoice"
    assert entry["pdf"]["s3"]["key"] == "m-1/invoice.pdf"


def test_stage_without_a_parse_output_fails_loudly():
    with pytest.raises(ValueError):
        bookkeeping.run_bookkeeping_stage({"type": "bookkeeping_stage"}, EVENT,
                                          "invoice-intake", steps={})


def test_the_shipped_invoice_intake_workflow_validates():
    # The canonical workflow file: its action chain and trigger filters
    # must satisfy the same save-time validation the API enforces, so the
    # file cannot rot away from what a live workflow would accept.
    import yaml

    workflow = yaml.safe_load((Path(__file__).resolve().parents[1] /
                               "workflows" / "invoice-intake.yaml").read_text())
    assert workflow["trigger"]["connector"] == "email"
    assert workflow["trigger"]["filters"]["route"]["equals"] == "invoice"
    validate_action_chain(workflow["actions"])


def test_the_shipped_invoice_intake_workflow_validates():
    # The canonical workflow file: its action chain and trigger filters
    # must satisfy the same save-time validation the API enforces, so the
    # file cannot rot away from what a live workflow would accept.
    import yaml

    workflow = yaml.safe_load((Path(__file__).resolve().parents[1] /
                               "workflows" / "invoice-intake.yaml").read_text())
    assert workflow["trigger"]["connector"] == "email"
    assert workflow["trigger"]["filters"]["route"]["equals"] == "invoice"
    validate_action_chain(workflow["actions"])
