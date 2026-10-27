"""dataops document_step: forward exactly the document a prior step made.

The invoice intake forwards the merge_pdfs result (a chosen receipt's
passthrough or a merged PDF) instead of every attachment, so DataOps
receives one document per email.
"""
import hashlib
import json
from unittest.mock import MagicMock, patch

import pytest

from src.dapier.engine.actions.dataops import run_dataops

import src.dapier.connectors  # noqa: F401  (import = registration)


BODY = b"merged-pdf-bytes"
EVENT = {
    "connector": "email", "event": "message.received",
    "id": "ev1", "occurred_at": "2026-10-12T09:00:00+00:00",
    "data": {
        "message_id": "msg-1", "route": "invoice", "subject": "Stripe receipt",
        "attachments": [{"filename": "invoice.pdf", "checksum": "sha256:" + "a" * 64,
                         "s3": {"bucket": "mail", "key": "msg/a.pdf"}},
                        {"filename": "Receipt.pdf", "checksum": "sha256:" + "c" * 64,
                         "s3": {"bucket": "mail", "key": "msg/c.pdf"}}],
    },
}
STEPS = {"route.then.make-document": {
    "output": {"merged": True, "parts": 2, "filename": "merged.pdf",
               "output": {"bucket": "artifacts", "key": "invoice-document/ev.pdf"}}}}
ACTION = {"type": "dataops", "connection_id": "dataops",
          "auth_secret_id": "dapier.dataops",
          "document_step": "route.then.make-document"}


def run(steps=STEPS, event=None, action=None):
    calls = {}

    def fake_request(url, payload, headers=None, timeout=10):
        calls.update({"url": url, "payload": payload, "headers": headers})
        return {}

    with patch("src.dapier.engine.actions.base.secrets_value", return_value="tok"), \
         patch("src.dapier.engine.actions.base._connected_connection",
               return_value={"provider": "dataops", "url": "https://ops.example.com"}), \
         patch("src.dapier.engine.actions.dataops._s3_bytes", return_value=BODY), \
         patch("src.dapier.engine.actions.dataops._stage_document",
               side_effect=lambda key, body, ct, checksum: f"s3://stage/{key}") as stage, \
         patch("src.dapier.engine.actions.base._json_request", side_effect=fake_request):
        output = run_dataops(action or ACTION, event or EVENT, steps=steps)
    return output, calls, stage


def test_the_intake_carries_only_the_selected_document():
    output, calls, stage = run()
    assert output == {}
    assert calls["url"] == "https://ops.example.com/api/v1/intake/email-documents"
    assert calls["headers"]["x-dataops-intake-secret"] == "tok"
    envelope = calls["payload"]
    assert envelope["messageId"] == "msg-1"
    assert envelope["recipientRoute"] == "invoice"
    assert envelope["subject"] == "Stripe receipt"
    assert envelope["receivedAt"] == "2026-10-12T09:00:00Z"
    assert len(envelope["documents"]) == 1
    document = envelope["documents"][0]
    assert document == {
        "kind": "attachment",
        "storageUri": document["storageUri"],
        "filename": "merged.pdf",
        "contentType": "application/pdf",
        "sizeBytes": len(BODY),
        "checksum": "sha256:" + hashlib.sha256(BODY).hexdigest(),
    }
    key = document["storageUri"].split("/stage/", 1)[1]
    assert key.startswith("email/") and key.endswith("/merged.pdf")
    assert stage.call_args.args[3] == "sha256:" + hashlib.sha256(BODY).hexdigest()


def test_a_passthrough_keeps_the_original_checksum():
    steps = {"route.then.make-document": {
        "output": {"merged": False, "filename": "Receipt.pdf",
                   "output": {"bucket": "mail", "key": "msg/c.pdf"}}}}
    _, calls, _ = run(steps=steps)
    document = calls["payload"]["documents"][0]
    assert document["filename"] == "Receipt.pdf"
    assert document["checksum"] == "sha256:" + hashlib.sha256(BODY).hexdigest()


def test_a_missing_document_reference_fails_loudly():
    with pytest.raises(ValueError, match="does not reference a stored file"):
        run(steps={})
    broken = {"route.then.make-document": {"output": {"result": {"nope": True}}}}
    with pytest.raises(ValueError, match="does not reference a stored file"):
        run(steps=broken)


def test_without_document_step_every_document_attachment_is_forwarded():
    _, calls, stage = run(action={k: v for k, v in ACTION.items() if k != "document_step"})
    envelope = calls["payload"]
    assert [d["filename"] for d in envelope["documents"]] == ["invoice.pdf", "Receipt.pdf"]
    assert envelope["documents"][0]["checksum"] == "sha256:" + "a" * 64
    assert stage.call_count == 2


def test_the_staging_key_never_carries_whitespace():
    """Live 2026-10-15 outage follow-up: DataOps parses the storageUri and
    rejects keys with whitespace, so a raw mail filename like
    "Rechnung 3513284497.pdf" must be sanitized in the staged object key
    while the manifest keeps the original filename verbatim."""
    steps = {"route.then.make-document": {
        "output": {"merged": False, "filename": "Rechnung 3513284497.pdf",
                   "output": {"bucket": "mail", "key": "msg/r.pdf"}}}}
    s3 = MagicMock()
    request = MagicMock(return_value={})
    with patch("src.dapier.engine.actions.base.secrets_value", return_value="tok"), \
         patch("src.dapier.engine.actions.base._connected_connection",
               return_value={"provider": "dataops", "url": "https://ops.example.com"}), \
         patch("src.dapier.engine.actions.dataops._s3_bytes", return_value=BODY), \
         patch("boto3.client", return_value=s3), \
         patch.dict("os.environ", {"DATAOPS_EMAIL_DOCUMENTS_BUCKET": "staging"}), \
         patch("src.dapier.engine.actions.base._json_request", request):
        run_dataops(ACTION, EVENT, steps=steps)
    key = s3.put_object.call_args.kwargs["Key"]
    assert key.startswith("transfer/email/")
    assert not any(ch.isspace() or ch == "?" for ch in key)
    assert key.endswith("/Rechnung_3513284497.pdf")
    document = request.call_args.args[1]["documents"][0]
    assert document["filename"] == "Rechnung 3513284497.pdf"
    assert document["storageUri"] == f"s3://staging/{key}"


def test_the_registry_lists_document_step():
    from src.dapier.connectors import registry

    spec = registry.ACTIONS["dataops"]
    assert "document_step" in spec.optional
    keys = [field["key"] for field in spec.fields]
    assert "document_step" in keys
