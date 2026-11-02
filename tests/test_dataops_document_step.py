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
         patch("src.dapier.engine.actions.base._s3_body", return_value=BODY), \
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
         patch("src.dapier.engine.actions.base._s3_body", return_value=BODY), \
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


class _NoSuchKey(Exception):
    response = {"Error": {"Code": "NoSuchKey"}}


RENDER_STEPS = {"route.else.render-pdf": {"output": {
    "job_id": "ev1:invoice-intake:render-pdf",
    "output": {"bucket": "artifacts", "key": "invoice-body/ev1.pdf"}}}}
RENDER_ACTION = {**ACTION, "document_step": "route.else.render-pdf"}


def test_missing_render_output_fails_with_clear_error_naming_the_job():
    from src.dapier.engine.actions import dataops
    with patch.object(dataops.base, "_s3_body", side_effect=_NoSuchKey()), \
         patch.object(dataops, "RENDER_WAIT_SECONDS", 0), \
         patch.object(dataops, "RENDER_POLL_SECONDS", 0):
        with pytest.raises(RuntimeError) as err:
            dataops._step_document("route.else.render-pdf", "invoice", "msg-1", RENDER_ACTION, RENDER_STEPS)
    assert "ev1:invoice-intake:render-pdf" in str(err.value)
    assert "dead-letter" in str(err.value)


def test_render_output_arriving_late_is_awaited():
    from src.dapier.engine.actions import dataops
    with patch.object(dataops.base, "_s3_body", side_effect=[_NoSuchKey(), BODY]), \
         patch.object(dataops, "RENDER_POLL_SECONDS", 0), \
         patch.object(dataops, "_stage_document", return_value="s3://stage/k"):
        doc = dataops._step_document("route.else.render-pdf", "invoice", "msg-1", RENDER_ACTION, RENDER_STEPS)
    assert doc["sizeBytes"] == len(BODY)


class _FakeStagingS3:
    """In-memory staging bucket: put_object stores, get_object replays."""
    def __init__(self):
        self.objects = {}

    def put_object(self, Bucket, Key, Body, **_):
        self.objects.setdefault((Bucket, Key), Body)

    def get_object(self, Bucket, Key):
        if (Bucket, Key) not in self.objects:
            raise _NoSuchKey()
        import io
        return {"Body": io.BytesIO(self.objects[(Bucket, Key)])}


def _manifest(fake, render_bytes, event, steps, action):
    from src.dapier.engine.actions import dataops
    with patch.dict("os.environ", {"DATAOPS_EMAIL_DOCUMENTS_BUCKET": "staging"}), \
         patch.object(dataops, "_s3", return_value=fake), \
         patch.object(dataops.base, "_s3_body", return_value=render_bytes):
        return dataops._email_intake_body(action, event, steps=steps)


def test_rerendered_document_step_submits_the_same_manifest():
    from src.dapier.engine.actions import dataops
    fake = _FakeStagingS3()
    first = _manifest(fake, b"%PDF render one", EVENT, RENDER_STEPS, RENDER_ACTION)
    second = _manifest(fake, b"%PDF render two", EVENT, RENDER_STEPS, RENDER_ACTION)
    assert first["documents"] == second["documents"]
    assert first["documents"][0]["checksum"] == "sha256:" + hashlib.sha256(b"%PDF render one").hexdigest()


def test_rerendered_email_pdf_submits_the_same_manifest():
    fake = _FakeStagingS3()
    def event(body, checksum):
        data = {**EVENT["data"], "attachments": [], "content_type": "application/pdf",
                "output": {"bucket": "artifacts", "key": "body.pdf"},
                "checksum": checksum}
        return {**EVENT, "data": data}
    first = _manifest(fake, b"one", event(b"one", hashlib.sha256(b"one").hexdigest()), None, ACTION | {"document_step": ""})
    second = _manifest(fake, b"two", event(b"two", hashlib.sha256(b"two").hexdigest()), None, ACTION | {"document_step": ""})
    assert first["documents"] == second["documents"]
    assert first["documents"][0]["kind"] == "rendered-email-pdf"


def test_different_email_content_gets_a_different_key():
    fake = _FakeStagingS3()
    other = {**EVENT, "data": {**EVENT["data"], "subject": "Another receipt"}}
    first = _manifest(fake, b"one", EVENT, RENDER_STEPS, RENDER_ACTION)
    second = _manifest(fake, b"two", other, RENDER_STEPS, RENDER_ACTION)
    assert first["documents"][0]["storageUri"] != second["documents"][0]["storageUri"]


def test_render_wait_is_180_seconds_with_backoff():
    from src.dapier.engine.actions import dataops
    assert dataops.RENDER_WAIT_SECONDS == 180
    clock = {"t": 0.0}
    sleeps = []
    def sleep(s):
        sleeps.append(s)
        clock["t"] += s
    with patch.object(dataops.base, "_s3_body", side_effect=_NoSuchKey()), \
         patch.object(dataops.time, "monotonic", side_effect=lambda: clock["t"]), \
         patch.object(dataops.time, "sleep", side_effect=sleep):
        with pytest.raises(RuntimeError, match="dead-letter"):
            dataops._await_document({"bucket": "b", "key": "k"}, "job-1", "render")
    assert 178 <= sum(sleeps) <= 181
    assert sleeps[-3] > sleeps[0]


def test_worker_timeout_covers_the_render_wait():
    import re, pathlib
    text = pathlib.Path("template.yaml").read_text()
    worker = text.split("  WorkerFunction:\n", 1)[1]
    timeout = int(re.search(r"Timeout: (\d+)", worker).group(1))
    queue = text.split("  EventQueue:\n", 1)[1]
    visibility = int(re.search(r"VisibilityTimeout: (\d+)", queue).group(1))
    assert timeout >= 180 + 60
    assert visibility >= timeout
