"""dataops action: push the event into a DataOps intake."""
import hashlib
import json
import mimetypes
import os
import re
import urllib.error
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

from ...connections import tokens
from ...connections.providers import dropbox_api
from . import base
from .templating import render


STAGING_PREFIX = "transfer/"

# DataOps owns sponsor validation, URL cleansing and lead identity/dedup.
# Dapier only wraps the raw extraction in the intake transport envelope.
LEADS_VERSION = "2026-10-10"
# Paths under the DataOps connection's https origin. The origin is stored
# on the connection (`dapier connections edit dataops --url https://…`);
# extraction_step selects leads vs documents.
LEADS_PATH = "/api/v1/intake/leads"
DOCUMENTS_PATH = "/api/v1/intake/email-documents"
_LEADS_RECEIVED_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?Z$")


def _s3():
    import boto3
    return boto3.client("s3")


def _safe_staging_key(key):
    """Make an object key referenceable by the intake's s3:// grammar.

    The intake parses the storageUri and rejects keys that contain
    whitespace or '?' ("Rechnung 3513284497.pdf" fails), so raw mail
    filenames cannot ride into the staging key as-is. Only the physical
    key is sanitized: the manifest's ``filename`` field keeps the original
    name, and the intake sanitizes that itself when it copies the object.
    The per-key digest keeps different contents apart, so two names that
    sanitize to the same segment can never collide."""
    return "/".join(re.sub(r"[\s?]+", "_", segment) for segment in key.split("/"))


def _stage_document(key, body, content_type, checksum):
    """Copy a document into the DataOps staging bucket and return its URI.

    The intake Head-verifies every document's S3 source and only accepts
    objects under the transfer/ prefix of its own documents bucket, so
    nothing can be referenced in place. The sha256 goes into object
    metadata because that is where the intake expects to find it."""
    bucket = os.environ["DATAOPS_EMAIL_DOCUMENTS_BUCKET"]
    digest = checksum.split(":", 1)[1] if checksum.startswith("sha256:") else checksum
    key = _safe_staging_key(f"{STAGING_PREFIX}{key}")
    _s3().put_object(
        Bucket=bucket, Key=key, Body=body, ContentType=content_type,
        Metadata={"sha256": digest}, ServerSideEncryption="AES256",
    )
    return f"s3://{bucket}/{key}"



def _received_at(value):
    """Second-precision UTC Z form — the only timestamp shape the DataOps
    intake contract accepts. Email events carry RFC 2822 Date headers and
    stored events carry +00:00 ISO timestamps; both are rejected as-is."""
    text = str(value or "").strip()
    if not text:
        return text
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        try:
            parsed = parsedate_to_datetime(text)
        except (TypeError, ValueError):
            return text
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def run_dataops(action, event, *, steps=None):
    value = base.secrets_value(action["auth_secret_id"])
    try:
        parsed = json.loads(value)
        token = parsed.get("token") or parsed.get("api_key")
    except json.JSONDecodeError:
        token = value
    if not token:
        raise ValueError("DataOps secret does not contain a token")
    try:
        return base._json_request(
            _target_url(action),
            _intake_body(action, event, steps=steps),
            headers={"x-dataops-intake-secret": token},
            timeout=action.get("timeout_seconds", 15),
        )
    except urllib.error.HTTPError as exc:
        # Exact duplicate intakes return 200 with their invoice previews.
        # A 409 means that the immutable source changed; swallowing it would
        # lose the preview and make a broken handoff appear successful.
        if exc.code == 409:
            raise ValueError(
                "DataOps source conflict: this message was filed with different "
                "content; repair the original source before retrying"
            ) from exc
        raise


def _dataops_connection(action):
    """The connected DataOps record named by the step's ``connection_id``."""
    connection_id = str(action.get("connection_id") or "").strip()
    if not connection_id:
        raise ValueError("dataops step requires connection_id (a DataOps connection)")
    connection = base._connected_connection(connection_id)
    if connection.get("provider") != "dataops":
        raise ValueError(f"connection {connection_id} is not a DataOps connection")
    return connection


def _target_url(action):
    """Intake URL from the DataOps connection origin plus the path this step uses."""
    origin = str(_dataops_connection(action).get("url") or "").rstrip("/")
    if not origin:
        raise ValueError(
            "DataOps connection has no url; set it with "
            "`dapier connections edit <ref> --url https://…`"
        )
    path = LEADS_PATH if _is_leads_action(action) else DOCUMENTS_PATH
    return origin + path

def _intake_body(action, event, *, steps=None):
    if _is_leads_action(action):
        return _leads_intake_body(action, event, steps=steps)
    if event.get("connector") == "dropbox":
        return _dropbox_intake_body(action, event, steps=steps)
    return _email_intake_body(action, event, steps=steps)


def _is_leads_action(action):
    """Sponsors-leads intake when the step names an extraction_step."""
    return bool(str(action.get("extraction_step") or "").strip())

def _dropbox_intake_body(action, event, *, steps=None):
    """Build a DataOps intake for a Dropbox file event.

    The intake contract references documents by S3 URI, so the file is
    downloaded and staged into the intake's documents bucket before the
    request is built. Dropbox content hashes are not file sha256s, so the
    checksum is computed over the downloaded bytes.
    """
    data = event.get("data", {})
    path = render(str(action.get("path") or data.get("path") or ""), event, steps)
    if not path:
        raise ValueError("dropbox intake requires a file path")
    filename = base._safe_filename(path.split("/")[-1])
    content_type = mimetypes.guess_type(filename)[0] or "application/octet-stream"
    dropbox_id = str(action.get("dropbox_connection_id") or "").strip()
    if not dropbox_id:
        raise ValueError("dropbox intake requires dropbox_connection_id")
    connection = dropbox_api.dropbox_connection(dropbox_id)
    access_token, _info = tokens.get_access_token(connection)
    body = dropbox_api.dropbox_download(access_token, path)
    checksum = f"sha256:{hashlib.sha256(body).hexdigest()}"
    key = f"dropbox/{str(event['id']).replace('/', '_')}/{filename}"
    storage_uri = _stage_document(key, body, content_type, checksum)
    return {
        "version": "2026-07-01",
        "messageId": event["id"],
        "recipientRoute": render(str(action.get("recipient_route") or "invoice"), event, steps),
        "from": "dropbox",
        "subject": filename,
        "receivedAt": _received_at(event["occurred_at"]),
        "documents": [{
            "kind": "attachment",
            "storageUri": storage_uri,
            "filename": filename,
            "contentType": content_type,
            "sizeBytes": len(body),
            "checksum": checksum,
        }],
    }

def _email_transfer_key(route, message_id, kind, index, filename, body, content_type, checksum):
    # DataOps fingerprints source URIs: retrying after a lost response must
    # submit the same manifest. Include the bytes so changed content cannot
    # overwrite a source object already accepted under this message identity.
    identity = [route.lower(), message_id.strip(), kind, index, filename,
                content_type, checksum, hashlib.sha256(body).hexdigest()]
    digest = hashlib.sha256(json.dumps(identity, separators=(",", ":")).encode()).hexdigest()
    return f"email/{digest}/{filename}"


def _email_intake_body(action, event, *, steps=None):
    data = event.get("data", {})
    source_data = data.get("source_event", {}).get("data", data)
    message_id = data.get("message_id") or source_data["message_id"]
    route = data.get("route") or source_data["route"]
    document_step = str(action.get("document_step") or "").strip()
    if document_step:
        # A prior step (merge_pdfs) already decided the one document this
        # email is about: forward exactly that, not every attachment.
        documents = [_step_document(document_step, route, message_id, action, steps)]
    else:
        documents = []
        for index, attachment in enumerate(data.get("attachments", [])):
            if not base.is_document_attachment(attachment):
                continue  # ToS/legal boilerplate never belongs in the document intake
            ref = attachment.get("s3") or {}
            if ref.get("bucket") and ref.get("key"):
                body = base._s3_body(ref)
                filename = base._safe_filename(attachment.get("filename") or "attachment")
                content_type = attachment.get("content_type") or "application/octet-stream"
                storage_uri = _stage_document(
                    _email_transfer_key(route, message_id, "attachment", index, filename,
                                        body, content_type, attachment["checksum"]),
                    body, content_type, attachment["checksum"])
                documents.append({
                    "kind": "attachment",
                    "storageUri": storage_uri,
                    "filename": filename,
                    "contentType": content_type,
                    "sizeBytes": len(body),
                    "checksum": attachment["checksum"],
                })
        output = data.get("output") or {}
        if output.get("bucket") and output.get("key"):
            body = base._s3_body(output)
            filename = base._safe_filename(action.get("filename", "invoice-email.pdf"))
            content_type = data.get("content_type", "application/pdf")
            checksum = f"sha256:{data['checksum']}"
            storage_uri = _stage_document(
                _email_transfer_key(route, message_id, "rendered-email-pdf", 0, filename,
                                    body, content_type, checksum),
                body, content_type, checksum)
            documents.append({
                "kind": "rendered-email-pdf",
                "storageUri": storage_uri,
                "filename": filename,
                "contentType": content_type,
                "sizeBytes": len(body),
                "checksum": checksum,
            })
    sender = source_data.get("sender", {})
    sender_value = (sender.get("addresses") or [sender.get("header") or "unknown@example.com"])[0]
    return {
        "version": "2026-07-01",
        "messageId": message_id,
        "recipientRoute": route,
        "from": sender_value,
        "subject": source_data.get("subject") or "Inbound email",
        "receivedAt": _received_at(source_data.get("date") or event["occurred_at"]),
        "documents": documents,
    }


def _step_document(ref, route, message_id, action, steps):
    """Stage the one document a prior step produced (a merge_pdfs output:
    ``{"filename", "output": {"bucket", "key"}}`` under the step's output)
    and build its intake document entry. The checksum is computed over the
    stored bytes, so passthrough documents keep the original attachment's."""
    node = (steps or {}).get(ref)
    output = node.get("output") if isinstance(node, dict) and isinstance(node.get("output"), dict) else {}
    document = output.get("output") if isinstance(output.get("output"), dict) else {}
    if not (isinstance(document, dict) and document.get("bucket") and document.get("key")):
        raise ValueError(f"dataops document_step '{ref}' does not reference a stored file")
    body = base._s3_body(document)
    filename = base._safe_filename(str(document.get("filename") or output.get("filename") or "").strip()
                                   or action.get("filename") or "invoice-document.pdf")
    checksum = f"sha256:{hashlib.sha256(body).hexdigest()}"
    storage_uri = _stage_document(
        _email_transfer_key(route, message_id, "attachment", 0, filename,
                            body, "application/pdf", checksum),
        body, "application/pdf", checksum)
    return {
        "kind": "attachment",
        "storageUri": storage_uri,
        "filename": filename,
        "contentType": "application/pdf",
        "sizeBytes": len(body),
        "checksum": checksum,
    }


def _extraction_data(action, steps):
    """The ai_complete reply shape for the sponsors step: prefer the
    configured ``extraction_step`` (default ``extract-sponsors``), else any
    step whose output.data carries a sponsors list."""
    names = []
    configured = str(action.get("extraction_step") or "").strip()
    if configured:
        names.append(configured)
    if "extract-sponsors" not in names:
        names.append("extract-sponsors")
    steps = steps or {}
    for name in names:
        node = steps.get(name)
        output = node.get("output") if isinstance(node, dict) else None
        data = output.get("data") if isinstance(output, dict) else None
        if isinstance(data, dict) and isinstance(data.get("sponsors"), list):
            if output.get("ok") is False:
                raise ValueError(
                    f"extraction step '{name}' did not produce structured data "
                    f"({data.get('error') or output.get('error') or 'ok: false'})")
            return data
    for name, node in steps.items():
        if name in names or not isinstance(node, dict):
            continue
        output = node.get("output")
        data = output.get("data") if isinstance(output, dict) else None
        if isinstance(data, dict) and isinstance(data.get("sponsors"), list):
            if isinstance(output, dict) and output.get("ok") is False:
                continue
            return data
    wanted = names[0] if names else "extract-sponsors"
    raise ValueError(
        f"leads intake needs structured sponsors from step '{wanted}' "
        "(ai_complete with a sponsors json_schema); no step output carries one")


def _leads_newsletter_name(data, fallback):
    raw = data.get("newsletter")
    if isinstance(raw, dict):
        for key in ("name", "title"):
            if str(raw.get(key) or "").strip():
                return raw[key]
        return fallback
    return raw or fallback


def _leads_intake_body(action, event, *, steps=None):
    """Wrap the raw extraction; all sponsor cleansing belongs to DataOps."""
    data = event.get("data", {})
    source_data = data.get("source_event", {}).get("data", data)
    message_id = data.get("message_id") or source_data.get("message_id")
    if not message_id:
        raise ValueError("leads intake requires an email message_id")
    sender = source_data.get("sender", {})
    sender_value = (sender.get("addresses") or [sender.get("header") or "unknown@example.com"])[0]
    subject = source_data.get("subject") or "Inbound email"
    received = _received_at(source_data.get("date") or event.get("occurred_at"))
    if not _LEADS_RECEIVED_RE.match(received or ""):
        raise ValueError(f"leads intake needs a parseable email date, got {received!r}")
    extraction = _extraction_data(action, steps)
    return {
        "version": LEADS_VERSION,
        "messageId": message_id,
        "newsletter": {
            "name": _leads_newsletter_name(extraction, subject),
            "sender": sender_value,
            "subject": subject,
            "receivedAt": received,
        },
        "sponsors": extraction["sponsors"],
    }
