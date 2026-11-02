"""Distinguish exact duplicate previews from conflicting source content."""
import io
import urllib.error
from unittest.mock import patch
import pytest

from src.dapier.engine.actions.dataops import run_dataops


def _http_error(code, reason="Conflict"):
    return urllib.error.HTTPError(
        "https://ops.example.test/api/v1/intake/email-documents",
        code, reason, hdrs=None, fp=io.BytesIO(b""),
    )


def _run(outcome):
    action = {"type": "dataops", "auth_secret_id": "dapier.dataops",
              "connection_id": "dataops"}
    with patch("src.dapier.engine.actions.base.secrets_value",
               return_value='{"token": "tok"}'), \
         patch("src.dapier.engine.actions.dataops._target_url",
               return_value="https://ops.example.test/api/v1/intake/email-documents"), \
         patch("src.dapier.engine.actions.dataops._intake_body",
               return_value={"messageId": "already-filed"}), \
         patch("src.dapier.engine.actions.base._json_request",
               side_effect=outcome if isinstance(outcome, Exception) else None,
               return_value=outcome):
        return run_dataops(action, {"id": "replay-1"})


def test_changed_source_409_is_a_failure():
    with pytest.raises(ValueError, match="DataOps source conflict"):
        _run(_http_error(409))


def test_exact_duplicate_retains_company_preview():
    response = {"status": "duplicate", "invoices": [{"counterparty": "Example Ltd"}]}
    assert _run(response) == response


def test_intake_500_still_fails():
    try:
        _run(_http_error(500, "Internal Server Error"))
    except urllib.error.HTTPError as exc:
        assert exc.code == 500
    else:
        raise AssertionError("expected HTTPError 500")


def test_different_document_with_staged_candidates_still_conflicts():
    """When staged candidates exist for (route, message_id), a genuinely
    different document (changed email content / render) must still fail with
    DataOps source conflict rather than resubmitting a stale candidate."""
    import hashlib
    from src.dapier.engine.actions import dataops
    from tests.test_dataops_document_step import _FakeStagingS3

    fake = _FakeStagingS3()
    orig_bytes = b"%PDF-1.4 original accepted invoice"
    orig_sha = hashlib.sha256(orig_bytes).hexdigest()
    orig_source = {"message_id": "msg-conflict-1", "route": "invoice", "subject": "Invoice 100 EUR"}
    orig_stable_rel = dataops._stable_transfer_key(
        "invoice", "msg-conflict-1", "rendered-email-pdf", "invoice-email.pdf",
        dataops._source_hash(orig_source),
    )
    orig_legacy_rel = dataops._email_transfer_key(
        "invoice", "msg-conflict-1", "rendered-email-pdf", 0,
        "invoice-email.pdf", orig_bytes, "application/pdf", f"sha256:{orig_sha}",
    )
    fake.put_object(
        Bucket="staging", Key=f"transfer/{orig_stable_rel}", Body=orig_bytes,
        Metadata={"sha256": orig_sha},
    )
    fake.put_object(
        Bucket="staging", Key=f"transfer/{orig_legacy_rel}", Body=orig_bytes,
        Metadata={"sha256": orig_sha},
    )

    changed_bytes = b"%PDF-1.4 altered invoice 999 EUR"
    changed_sha = hashlib.sha256(changed_bytes).hexdigest()
    changed_event = {
        "connector": "email", "event": "message.rendered",
        "id": "ev-conflict:render", "occurred_at": "2026-11-02T12:00:00Z",
        "data": {
            "source_event": {
                "data": {"message_id": "msg-conflict-1", "route": "invoice", "subject": "Invoice 999 EUR"},
            },
            "message_id": "msg-conflict-1",
            "route": "invoice",
            "content_type": "application/pdf",
            "output": {"bucket": "artifacts", "key": "invoice-body/ev-conflict.pdf"},
            "checksum": changed_sha,
        },
    }
    action = {
        "type": "dataops", "auth_secret_id": "dapier.dataops",
        "connection_id": "dataops", "filename": "invoice-email.pdf",
    }

    seen_uris = []

    def fake_request(url, payload, headers=None, timeout=15):
        uri = payload["documents"][0]["storageUri"]
        seen_uris.append(uri)
        if uri.endswith(orig_stable_rel) or uri.endswith(orig_legacy_rel):
            return {"status": "duplicate", "invoices": [{"id": "inv-1"}]}
        raise _http_error(409)

    with patch.dict("os.environ", {"DATAOPS_EMAIL_DOCUMENTS_BUCKET": "staging"}), \
         patch.object(dataops, "_s3", return_value=fake), \
         patch.object(dataops.base, "_s3_body", return_value=changed_bytes), \
         patch("src.dapier.engine.actions.base.secrets_value", return_value='{"token": "tok"}'), \
         patch("src.dapier.engine.actions.dataops._target_url",
               return_value="https://ops.example.test/api/v1/intake/email-documents"), \
         patch("src.dapier.engine.actions.base._json_request", side_effect=fake_request):
        with pytest.raises(ValueError, match="DataOps source conflict"):
            run_dataops(action, changed_event)

    assert len(seen_uris) == 1
    assert not seen_uris[0].endswith(orig_stable_rel)
    assert not seen_uris[0].endswith(orig_legacy_rel)
