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
    """In-memory staging bucket: put_object stores, get_object/head/list replay."""
    def __init__(self):
        self.objects = {}
        self.meta = {}
        self._tick = 0

    def put_object(self, Bucket, Key, Body, Metadata=None, **_):
        self._tick += 1
        self.objects.setdefault((Bucket, Key), Body)
        self.meta.setdefault((Bucket, Key), {
            "Metadata": dict(Metadata or {}),
            "LastModified": f"2026-11-02T08:51:{self._tick:02d}Z",
        })

    def get_object(self, Bucket, Key):
        if (Bucket, Key) not in self.objects:
            raise _NoSuchKey()
        import io
        return {"Body": io.BytesIO(self.objects[(Bucket, Key)])}

    def head_object(self, Bucket, Key):
        if (Bucket, Key) not in self.objects:
            raise _NoSuchKey()
        return self.meta.get((Bucket, Key), {"Metadata": {}})

    def list_objects_v2(self, Bucket, Prefix="", **_):
        contents = []
        for (b, k), info in self.meta.items():
            if b == Bucket and k.startswith(Prefix):
                contents.append({"Key": k, "LastModified": info["LastModified"]})
        return {"Contents": contents, "IsTruncated": False}


def _manifest(fake, render_bytes, event, steps, action):
    from src.dapier.engine.actions import dataops
    with patch.dict("os.environ", {"DATAOPS_EMAIL_DOCUMENTS_BUCKET": "staging"}), \
         patch.object(dataops, "_s3", return_value=fake), \
         patch.object(dataops.base, "_s3_body", return_value=render_bytes):
        return dataops._email_intake_body(action, event, steps=steps)


def test_rerendered_document_step_submits_the_same_manifest():
    fake = _FakeStagingS3()
    first = _manifest(fake, b"%PDF render one", EVENT, RENDER_STEPS, RENDER_ACTION)
    second = _manifest(fake, b"%PDF render two", EVENT, RENDER_STEPS, RENDER_ACTION)
    assert first["documents"] == second["documents"]
    assert first["documents"][0]["kind"] == "rendered-email-pdf"
    assert first["documents"][0]["checksum"] == "sha256:" + hashlib.sha256(b"%PDF render one").hexdigest()


def test_document_step_and_body_completion_share_rendered_manifest():
    """invoice-intake (body-to-dataops with document_step) and
    invoice-body-completion (intake-rendered without document_step) must
    produce identical manifests for the same email so the pair never 409s."""
    fake = _FakeStagingS3()
    intake_action = {**RENDER_ACTION, "filename": "invoice-email.pdf"}
    completion_action = {**ACTION, "document_step": "", "filename": "invoice-email.pdf"}
    completion_event = {
        "connector": "email", "event": "message.rendered",
        "id": "ev1:render", "occurred_at": "2026-10-12T09:01:00+00:00",
        "data": {
            "source_event": EVENT,
            "message_id": EVENT["data"]["message_id"],
            "route": EVENT["data"]["route"],
            "content_type": "application/pdf",
            "output": {"bucket": "artifacts", "key": "invoice-body/ev1.pdf"},
            "checksum": hashlib.sha256(b"%PDF render two").hexdigest(),
        },
    }
    first = _manifest(fake, b"%PDF render one", EVENT, RENDER_STEPS, intake_action)
    second = _manifest(fake, b"%PDF render two", completion_event, None, completion_action)
    assert first["documents"] == second["documents"]
    assert first["documents"][0]["kind"] == "rendered-email-pdf"


def test_replay_attachment_restaging_keeps_same_rendered_manifest():
    """Replaying an email with non-PDF attachments re-stages them under
    relay-attachments/replay-<uuid>/... and drops inbound_message_id; the
    rendered email PDF identity must remain unchanged."""
    fake = _FakeStagingS3()
    original_event = {
        **EVENT,
        "data": {
            **EVENT["data"],
            "inbound_message_id": "ses-orig-1",
            "route_address": "invoice@in.example.com",
            "s3": {"bucket": "raw", "key": "inbound/ses-orig-1"},
            "attachments": [
                {"filename": "logo.png", "content_type": "image/png",
                 "checksum": "sha256:" + "b" * 64,
                 "s3": {"bucket": "artifacts", "key": "relay-attachments/ses-orig-1/attachments/0/logo.png"}},
            ],
        },
    }
    replayed_event = {
        **EVENT,
        "id": "replay-999",
        "data": {
            **EVENT["data"],
            "route_address": "invoice",
            "attachments": [
                {"filename": "logo.png", "content_type": "image/png",
                 "checksum": "sha256:" + "b" * 64,
                 "s3": {"bucket": "artifacts", "key": "relay-attachments/replay-999/attachments/0/logo.png"}},
            ],
        },
    }
    intake_action = {**RENDER_ACTION, "filename": "invoice-email.pdf"}
    first = _manifest(fake, b"%PDF original", original_event, RENDER_STEPS, intake_action)
    second = _manifest(fake, b"%PDF replayed", replayed_event, RENDER_STEPS, intake_action)
    assert first["documents"] == second["documents"]


def test_replay_reuses_legacy_byte_keyed_rendered_pdf():
    """Messages first accepted before a771da4 were staged under the
    byte-derived _email_transfer_key. Even if a later failed replay staged a
    second render under a newer key, _prior_staged_rendered selects the
    oldest staged object (matching DataOps first-write-wins)."""
    from src.dapier.engine.actions import dataops
    fake = _FakeStagingS3()
    first_bytes = b"%PDF-1.4 original accepted render at 08:51Z"
    first_sha = hashlib.sha256(first_bytes).hexdigest()
    first_checksum = f"sha256:{first_sha}"
    legacy_rel = dataops._email_transfer_key(
        "invoice", "msg-1", "rendered-email-pdf", 0,
        "invoice-email.pdf", first_bytes, "application/pdf", first_checksum,
    )
    fake.put_object(
        Bucket="staging", Key=f"transfer/{legacy_rel}", Body=first_bytes,
        Metadata={"sha256": first_sha},
    )
    # Later failed replay staged a different render under a newer key:
    later_bytes = b"%PDF-1.4 later failed replay at 10:29Z"
    later_sha = hashlib.sha256(later_bytes).hexdigest()
    later_rel = dataops._stable_transfer_key(
        "invoice", "msg-1", "attachment", "invoice-email.pdf",
        dataops._legacy_source_hash(EVENT["data"]),
    )
    fake.put_object(
        Bucket="staging", Key=f"transfer/{later_rel}", Body=later_bytes,
        Metadata={"sha256": later_sha},
    )

    intake_action = {**RENDER_ACTION, "filename": "invoice-email.pdf"}
    manifest = _manifest(fake, b"%PDF-1.4 third render", EVENT, RENDER_STEPS, intake_action)
    assert manifest["documents"] == [{
        "kind": "rendered-email-pdf",
        "storageUri": f"s3://staging/transfer/{legacy_rel}",
        "filename": "invoice-email.pdf",
        "contentType": "application/pdf",
        "sizeBytes": len(first_bytes),
        "checksum": first_checksum,
    }]


def test_rendered_409_recovers_by_trying_remaining_staged_candidates():
    """If the initial staged candidate 409s (e.g. an unaccepted object had an
    earlier timestamp or different kind), run_dataops retries remaining staged
    candidates for (route, message_id) and returns DataOps's 200 duplicate."""
    import io
    import urllib.error
    from src.dapier.engine.actions import dataops

    fake = _FakeStagingS3()
    unaccepted_bytes = b"%PDF unaccepted early stage"
    unaccepted_sha = hashlib.sha256(unaccepted_bytes).hexdigest()
    unaccepted_rel = dataops._email_transfer_key(
        "invoice", "msg-1", "rendered-email-pdf", 0,
        "invoice-email.pdf", unaccepted_bytes, "application/pdf", f"sha256:{unaccepted_sha}",
    )
    fake.put_object(
        Bucket="staging", Key=f"transfer/{unaccepted_rel}", Body=unaccepted_bytes,
        Metadata={"sha256": unaccepted_sha},
    )
    accepted_bytes = b"%PDF accepted second stage"
    accepted_sha = hashlib.sha256(accepted_bytes).hexdigest()
    accepted_rel = dataops._email_transfer_key(
        "invoice", "msg-1", "rendered-email-pdf", 0,
        "invoice-email.pdf", accepted_bytes, "application/pdf", f"sha256:{accepted_sha}",
    )
    fake.put_object(
        Bucket="staging", Key=f"transfer/{accepted_rel}", Body=accepted_bytes,
        Metadata={"sha256": accepted_sha},
    )

    seen_uris = []

    def fake_request(url, payload, headers=None, timeout=15):
        uri = payload["documents"][0]["storageUri"]
        seen_uris.append(uri)
        if uri.endswith(accepted_rel):
            return {"status": "duplicate", "invoices": [{"id": "inv-1", "amount": "219.99"}]}
        raise urllib.error.HTTPError(url, 409, "Conflict", hdrs=None, fp=io.BytesIO(b""))

    intake_action = {**RENDER_ACTION, "filename": "invoice-email.pdf"}
    with patch.dict("os.environ", {"DATAOPS_EMAIL_DOCUMENTS_BUCKET": "staging"}), \
         patch.object(dataops, "_s3", return_value=fake), \
         patch("src.dapier.engine.actions.base.secrets_value", return_value="tok"), \
         patch("src.dapier.engine.actions.base._connected_connection",
               return_value={"provider": "dataops", "url": "https://ops.example.com"}), \
         patch("src.dapier.engine.actions.base._json_request", side_effect=fake_request):
        result = run_dataops(intake_action, EVENT, steps=RENDER_STEPS)

    assert result == {"status": "duplicate", "invoices": [{"id": "inv-1", "amount": "219.99"}]}
    assert seen_uris[0].endswith(unaccepted_rel)
    assert seen_uris[-1].endswith(accepted_rel)


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
