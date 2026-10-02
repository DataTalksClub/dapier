"""dataops action: push the event into a DataOps intake."""
import hashlib
import json
import mimetypes
import os
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

from ...connections import tokens
from . import base, dropbox
from .templating import render


STAGING_PREFIX = "transfer/"


def _s3():
    import boto3
    return boto3.client("s3")


def _stage_document(key, body, content_type, checksum):
    """Copy a document into the DataOps staging bucket and return its URI.

    The intake Head-verifies every document's S3 source and only accepts
    objects under the transfer/ prefix of its own documents bucket, so
    nothing can be referenced in place. The sha256 goes into object
    metadata because that is where the intake expects to find it."""
    bucket = os.environ["DATAOPS_EMAIL_DOCUMENTS_BUCKET"]
    digest = checksum.split(":", 1)[1] if checksum.startswith("sha256:") else checksum
    key = f"{STAGING_PREFIX}{key}"
    _s3().put_object(
        Bucket=bucket, Key=key, Body=body, ContentType=content_type,
        Metadata={"sha256": digest}, ServerSideEncryption="AES256",
    )
    return f"s3://{bucket}/{key}"


def _s3_bytes(bucket, key):
    return _s3().get_object(Bucket=bucket, Key=key)["Body"].read()


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
    return base._json_request(
        action.get("url") or os.environ[action.get("url_env", "DATAOPS_INTAKE_URL")],
        _intake_body(action, event, steps=steps),
        headers={"x-dataops-intake-secret": token},
        timeout=action.get("timeout_seconds", 15),
    )

def _intake_body(action, event, *, steps=None):
    if event.get("connector") == "dropbox":
        return _dropbox_intake_body(action, event, steps=steps)
    return _email_intake_body(action, event)

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
    connection = dropbox._dropbox_connection(action["connection_id"])
    access_token, _info = tokens.get_access_token(connection)
    body = dropbox._dropbox_download(access_token, path)
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


def _email_intake_body(action, event):
    data = event.get("data", {})
    source_data = data.get("source_event", {}).get("data", data)
    message_id = data.get("message_id") or source_data["message_id"]
    route = data.get("route") or source_data["route"]
    documents = []
    for index, attachment in enumerate(data.get("attachments", [])):
        ref = attachment.get("s3") or {}
        if ref.get("bucket") and ref.get("key"):
            body = _s3_bytes(ref["bucket"], ref["key"])
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
        body = _s3_bytes(output["bucket"], output["key"])
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
