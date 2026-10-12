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

# Newsletter-sponsors leads intake (DataOps contract 2026-10-10). Dapier is
# only the pipe: one LLM extraction plus this formatter; sponsor records,
# dedup across emails, outreach and review live in DataOps. The intake
# accepts 1-20 sponsors per POST and dedups per lead on
# sha256("lead:"+messageId+name+url), so this builder normalizes within one
# email only (trim/collapse, https-only URLs, within-email dedup, caps) and
# fails loudly on anything the intake would reject.
LEADS_VERSION = "2026-10-10"
# Paths under the DataOps connection's https origin. The origin is stored
# on the connection (`dapier connections edit dataops --url https://…`);
# extraction_step selects leads vs documents.
LEADS_PATH = "/api/v1/intake/leads"
DOCUMENTS_PATH = "/api/v1/intake/email-documents"
_LEADS_RECEIVED_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?Z$")
_LEADS_WS_RE = re.compile(r"\s+")
MAX_LEADS_SPONSORS = 20


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
    try:
        return base._json_request(
            _target_url(action),
            _intake_body(action, event, steps=steps),
            headers={"x-dataops-intake-secret": token},
            timeout=action.get("timeout_seconds", 15),
        )
    except urllib.error.HTTPError as exc:
        # Intake is idempotent per (recipientRoute, messageId). A replay of
        # an already-filed document is 409, not a failed run.
        if exc.code == 409:
            return {"already_exists": True, "status": 409}
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
    body = _s3_bytes(document["bucket"], document["key"])
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


def _collapse_ws(text):
    return _LEADS_WS_RE.sub(" ", str(text or "")).strip()


def _leads_norm_name(name):
    return _collapse_ws(name).lower()


def _leads_norm_url(url):
    """The intake's identity form: lowercase host, no default port, no
    trailing slash, no fragment. Returns None when there is no usable URL."""
    text = str(url or "").strip()
    if not text:
        return None
    if not re.match(r"^https://", text, flags=re.I):
        return None
    if re.search(r"\s", text) or "@" in text.split("/", 3)[2]:
        return None
    lowered = re.sub(r"^https://", "https://", text, flags=re.I)
    host_rest = lowered[len("https://"):]
    host, _, rest = host_rest.partition("/")
    host = host.lower()
    if host.endswith(":443"):
        host = host[:-4]
    rest = rest.split("#", 1)[0].rstrip("/")
    return f"https://{host}" + (f"/{rest}" if rest else "")


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
                return _collapse_ws(raw[key])[:200]
    elif str(raw or "").strip():
        return _collapse_ws(raw)[:200]
    return _collapse_ws(fallback)[:200] or "Newsletter"


def _leads_intake_body(action, event, *, steps=None):
    """Build the DataOps sponsors-leads envelope (contract 2026-10-10).

    Event metadata (messageId/sender/subject/date) comes from the email
    event; the newsletter name and sponsor mentions come from the
    extraction step. Normalization is within-email only — cross-email
    dedup is the intake's idempotency rule. Raises ValueError (fails the
    step) on anything the intake would reject, so problems read loudly
    instead of posting a 4xx the autoretry would pace pointlessly."""
    data = event.get("data", {})
    source_data = data.get("source_event", {}).get("data", data)
    message_id = str(data.get("message_id") or source_data.get("message_id") or "").strip()
    if not message_id or len(message_id) > 500:
        raise ValueError("leads intake requires an email message_id (max 500 chars)")
    sender = source_data.get("sender", {})
    sender_value = (sender.get("addresses") or [sender.get("header") or "unknown@example.com"])[0]
    sender_value = _collapse_ws(sender_value)[:320] or "unknown@example.com"
    subject = _collapse_ws(source_data.get("subject") or "Inbound email")[:500] or "Inbound email"
    received = _received_at(source_data.get("date") or event.get("occurred_at"))
    if not _LEADS_RECEIVED_RE.match(received or ""):
        raise ValueError(f"leads intake needs a parseable email date, got {received!r}")

    extraction = _extraction_data(action, steps)
    raw_sponsors = extraction.get("sponsors") or []
    sponsors = []
    seen = set()
    for entry in raw_sponsors:
        if not isinstance(entry, dict):
            continue
        name = _collapse_ws(entry.get("name"))[:200]
        if not name:
            continue
        url = _leads_norm_url(entry.get("url"))
        # Same link under different name readings is one sponsor: dedup on
        # the URL when there is one, else on the normalized name.
        key = url if url else f"name:{_leads_norm_name(name)}"
        if key in seen:
            continue
        seen.add(key)
        sponsor = {"name": name}
        if url:
            sponsor["url"] = url
        # Category passes through (truncated) for the intake's triage; the
        # extraction schema already constrains it to the known taxonomy, so
        # an unknown value rides along rather than failing the forward.
        category = _collapse_ws(entry.get("category"))[:40]
        if category:
            sponsor["category"] = category
        blurb = _collapse_ws(entry.get("blurb"))[:2000]
        if blurb:
            sponsor["blurb"] = blurb
        context = _collapse_ws(entry.get("context"))[:2000]
        if context:
            sponsor["context"] = context
        sponsors.append(sponsor)
    if not sponsors:
        raise ValueError("leads intake has no sponsors to forward (extraction found none)")
    if len(sponsors) > MAX_LEADS_SPONSORS:
        raise ValueError(
            f"leads intake accepts at most {MAX_LEADS_SPONSORS} sponsors, got {len(sponsors)}")
    return {
        "version": LEADS_VERSION,
        "messageId": message_id,
        "newsletter": {
            "name": _leads_newsletter_name(extraction, subject),
            "sender": sender_value,
            "subject": subject,
            "receivedAt": received,
        },
        "sponsors": sponsors,
    }
