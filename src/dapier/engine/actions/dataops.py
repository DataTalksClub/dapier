"""dataops action: push the event into a DataOps intake."""
import hashlib
import json
import mimetypes
import os
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from uuid import uuid4

from ...connections import tokens
from . import base, dropbox


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


def run_dataops(action, event):
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
        _intake_body(action, event),
        headers={"x-dataops-intake-secret": token},
        timeout=action.get("timeout_seconds", 15),
    )

def _intake_body(action, event):
    if event.get("connector") == "dropbox":
        return _dropbox_intake_body(action, event)
    return _email_intake_body(action, event)

def _dropbox_intake_body(action, event):
    """Build a DataOps intake for a Dropbox file event.

    The intake contract references documents by S3 URI, so the file is
    downloaded and staged into the intake's documents bucket before the
    request is built. Dropbox content hashes are not file sha256s, so the
    checksum is computed over the downloaded bytes.
    """
    data = event.get("data", {})
    path = data.get("path")
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
        "recipientRoute": "dropbox-upload",
        "from": "dropbox",
        "subject": filename,
        "receivedAt": _received_at(event["occurred_at"]),
        "documents": [{
            "kind": "dropbox-file",
            "storageUri": storage_uri,
            "filename": filename,
            "contentType": content_type,
            "sizeBytes": len(body),
            "checksum": checksum,
        }],
    }

def _email_intake_body(action, event):
    data = event.get("data", {})
    documents = []
    for attachment in data.get("attachments", []):
        ref = attachment.get("s3") or {}
        if ref.get("bucket") and ref.get("key"):
            body = _s3_bytes(ref["bucket"], ref["key"])
            filename = base._safe_filename(attachment.get("filename") or "attachment")
            content_type = attachment.get("content_type") or "application/octet-stream"
            storage_uri = _stage_document(
                f"{uuid4().hex}/{filename}", body, content_type, attachment["checksum"])
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
            f"{uuid4().hex}/{filename}", body, content_type, checksum)
        documents.append({
            "kind": "rendered-email-pdf",
            "storageUri": storage_uri,
            "filename": filename,
            "contentType": content_type,
            "sizeBytes": len(body),
            "checksum": checksum,
        })
    source_data = data.get("source_event", {}).get("data", data)
    sender = source_data.get("sender", {})
    sender_value = (sender.get("addresses") or [sender.get("header") or "unknown@example.com"])[0]
    return {
        "version": "2026-07-01",
        "messageId": data.get("message_id") or source_data["message_id"],
        "recipientRoute": data.get("route") or source_data["route"],
        "from": sender_value,
        "subject": source_data.get("subject") or "Inbound email",
        "receivedAt": _received_at(source_data.get("date") or event["occurred_at"]),
        "documents": documents,
    }
