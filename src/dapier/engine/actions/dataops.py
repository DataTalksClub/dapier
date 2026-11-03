"""dataops action: push the event into a DataOps intake."""
import hashlib
import json
import mimetypes
import os
import re
import time
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
    url = _target_url(action)
    body = _intake_body(action, event, steps=steps)
    headers = {"x-dataops-intake-secret": token}
    timeout = action.get("timeout_seconds", 15)
    try:
        return base._json_request(url, body, headers=headers, timeout=timeout)
    except urllib.error.HTTPError as exc:
        # Exact duplicate intakes return 200 with their invoice previews.
        # A 409 means that the immutable source changed; swallowing it would
        # lose the preview and make a broken handoff appear successful.
        if exc.code == 409:
            recovered = _recover_rendered_conflict(
                url, body, action, event, steps=steps,
                headers=headers, timeout=timeout)
            if recovered is not None:
                return recovered
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

RENDER_RESULT_FIELDS = {"output", "checksum", "content_type", "job_id", "size_bytes", "sizeBytes"}
VOLATILE_SOURCE_FIELDS = RENDER_RESULT_FIELDS | {
    "inbound_message_id", "route_address", "recipients", "s3", "raw_mime",
}


def _legacy_source_hash(source_data):
    """Pre-normalization source hash (a771da4), kept for staged-candidate lookup."""
    stable = {k: v for k, v in source_data.items() if k not in RENDER_RESULT_FIELDS}
    blob = json.dumps(stable, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(blob.encode()).hexdigest()


def _source_hash(source_data):
    """Stable identity of the original email (not of any render output or replay envelope)."""
    # Without a source_event the render result is merged into the same dict,
    # and replays re-stage attachments under relay-attachments/replay-<uuid>/...
    # and drop/rebuild envelope transport fields: strip those so a replay
    # hashes to the same identity as the original delivery.
    stable = {}
    for k, v in source_data.items():
        if k in VOLATILE_SOURCE_FIELDS:
            continue
        if k == "attachments" and isinstance(v, list):
            v = [{ak: av for ak, av in item.items() if ak != "s3"}
                 if isinstance(item, dict) else item for item in v]
        stable[k] = v
    blob = json.dumps(stable, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(blob.encode()).hexdigest()


def _stable_transfer_key(route, message_id, kind, filename, source_hash):
    # Rendered PDFs are not byte-deterministic, and DataOps fingerprints the
    # manifest (checksum + storageUri) per (route, messageId). Derive the key
    # from the email identity only so a re-render reuses the first staged copy.
    identity = [route.lower(), message_id.strip(), kind, filename, source_hash]
    digest = hashlib.sha256(json.dumps(identity, separators=(",", ":")).encode()).hexdigest()
    return f"email/{digest}/{filename}"


def _s3_error_code(exc):
    return str(getattr(exc, "response", {}).get("Error", {}).get("Code") or "")


def _already_staged(key):
    """Return (uri, body, checksum) of an object staged earlier under ``key``,
    or None. Replays must submit the first delivery's bytes and checksum."""
    bucket = os.environ["DATAOPS_EMAIL_DOCUMENTS_BUCKET"]
    full = _safe_staging_key(f"{STAGING_PREFIX}{key}")
    try:
        obj = _s3().get_object(Bucket=bucket, Key=full)
    except Exception as exc:
        if _s3_error_code(exc) in ("NoSuchKey", "404", "NotFound"):
            return None
        raise
    body = obj["Body"].read()
    return f"s3://{bucket}/{full}", body, f"sha256:{hashlib.sha256(body).hexdigest()}"


def _stage_document_first_write(key, body, content_type, checksum, source_hash):
    """Create a stable rendered-document object without replacing its first bytes.

    Concurrent replays can both miss the initial lookup. S3's conditional
    write makes the first staged render win; a losing writer reuses the bytes
    and checksum already stored at that identity key.
    """
    bucket = os.environ["DATAOPS_EMAIL_DOCUMENTS_BUCKET"]
    full = _safe_staging_key(f"{STAGING_PREFIX}{key}")
    digest = checksum.split(":", 1)[1] if checksum.startswith("sha256:") else checksum
    metadata = {"sha256": digest}
    if source_hash:
        metadata["source_hash"] = source_hash
    uri = f"s3://{bucket}/{full}"
    s3 = _s3()
    for attempt in range(3):
        try:
            s3.put_object(
                Bucket=bucket, Key=full, Body=body, ContentType=content_type,
                Metadata=metadata, ServerSideEncryption="AES256", IfNoneMatch="*",
            )
            return uri, body, f"sha256:{digest}"
        except Exception as exc:
            code = _s3_error_code(exc)
            if code not in ("PreconditionFailed", "412", "ConditionalRequestConflict", "409"):
                raise
            existing = _already_staged(key)
            if existing is not None:
                return existing
            if code in ("ConditionalRequestConflict", "409") and attempt < 2:
                continue
            raise
    raise RuntimeError(f"could not stage rendered document at s3://{bucket}/{full}")


def _email_transfer_key_for_digest(route, message_id, kind, index, filename, content_type, checksum, body_digest):
    identity = [route.lower(), message_id.strip(), kind, index, filename,
                content_type, checksum, body_digest]
    digest = hashlib.sha256(json.dumps(identity, separators=(",", ":")).encode()).hexdigest()
    return f"email/{digest}/{filename}"


def _email_transfer_key(route, message_id, kind, index, filename, body, content_type, checksum):
    # DataOps fingerprints source URIs: retrying after a lost response must
    # submit the same manifest. Include the bytes so changed content cannot
    # overwrite a source object already accepted under this message identity.
    return _email_transfer_key_for_digest(
        route, message_id, kind, index, filename,
        content_type, checksum, hashlib.sha256(body).hexdigest())


STAGING_SCAN_MAX_PAGES = 2
STAGING_SCAN_MAX_OBJECTS = 200
STAGING_SCAN_MAX_PROBES = 10
STAGING_SCAN_TIMEOUT_SECONDS = 5.0
CONFLICT_RECOVERY_MAX_CANDIDATES = 3
CONFLICT_RECOVERY_TIMEOUT_SECONDS = 10.0


def _prior_staged_rendered(route, message_id, filename, content_type="application/pdf",
                           source_data=None, *, source_hash=None, body_sha256=None,
                           kind="rendered-email-pdf", first_only=False):
    """Discover rendered-PDF objects already staged for ``(route, message_id)``
    in DATAOPS_EMAIL_DOCUMENTS_BUCKET that provably represent the same
    message/document (same message_id, same ``kind``, and matching source or
    content hash).

    Prefers direct key lookups (stable keys for current/legacy source_hash and
    the direct byte-derived key when ``body_sha256`` is known) before falling
    back to a count- and time-bounded prefix scan.
    """
    bucket = os.environ.get("DATAOPS_EMAIL_DOCUMENTS_BUCKET")
    if not bucket or not route or not message_id:
        return []
    s3 = _s3()
    hashes = []
    if source_hash:
        hashes.append(source_hash)
    if isinstance(source_data, dict):
        for fn in (_source_hash, _legacy_source_hash):
            h = fn(source_data)
            if h not in hashes:
                hashes.append(h)

    direct_rel_keys = []
    stable_full_keys = set()
    for sh in hashes:
        rel = _stable_transfer_key(route, message_id, kind, filename, sh)
        full = _safe_staging_key(f"{STAGING_PREFIX}{rel}")
        if rel not in direct_rel_keys:
            direct_rel_keys.append(rel)
        stable_full_keys.add(full)

    sha_hex = ""
    if body_sha256:
        sha_hex = (str(body_sha256).split(":", 1)[1]
                   if str(body_sha256).startswith("sha256:") else str(body_sha256))
    if sha_hex:
        legacy_rel = _email_transfer_key_for_digest(
            route, message_id, kind, 0, filename,
            content_type, f"sha256:{sha_hex}", sha_hex)
        if legacy_rel not in direct_rel_keys:
            direct_rel_keys.append(legacy_rel)
        stable_full_keys.add(_safe_staging_key(f"{STAGING_PREFIX}{legacy_rel}"))

    candidates = []
    seen_uris = set()
    for rel in direct_rel_keys:
        staged = _already_staged(rel)
        if staged:
            storage_uri, body, checksum = staged
            if storage_uri not in seen_uris:
                seen_uris.add(storage_uri)
                candidates.append({
                    "kind": kind,
                    "storageUri": storage_uri,
                    "filename": filename,
                    "contentType": content_type,
                    "sizeBytes": len(body),
                    "checksum": checksum,
                })
                if first_only or len(candidates) >= CONFLICT_RECOVERY_MAX_CANDIDATES:
                    return candidates

    if not hasattr(s3, "list_objects_v2"):
        return candidates

    deadline = time.monotonic() + STAGING_SCAN_TIMEOUT_SECONDS
    items = []
    token = None
    for _ in range(STAGING_SCAN_MAX_PAGES):
        if time.monotonic() >= deadline or len(items) >= STAGING_SCAN_MAX_OBJECTS:
            break
        kwargs = {
            "Bucket": bucket,
            "Prefix": f"{STAGING_PREFIX}email/",
            "MaxKeys": STAGING_SCAN_MAX_OBJECTS - len(items),
        }
        if token:
            kwargs["ContinuationToken"] = token
        page = s3.list_objects_v2(**kwargs)
        if not isinstance(page, dict):
            break
        contents = page.get("Contents")
        if isinstance(contents, list):
            for item in contents:
                if isinstance(item, dict):
                    items.append(item)
                    if len(items) >= STAGING_SCAN_MAX_OBJECTS:
                        break
        if not page.get("IsTruncated") or not page.get("NextContinuationToken"):
            break
        token = page["NextContinuationToken"]

    if not items:
        return candidates

    safe_suffix = "/" + _safe_staging_key(filename)
    matching_items = [
        item for item in items
        if isinstance(item.get("Key"), str) and item["Key"].endswith(safe_suffix)
    ]
    matching_items.sort(key=lambda item: (
        str(item.get("LastModified") or ""), item.get("Key") or "",
    ))
    probes = 0
    for item in matching_items:
        if time.monotonic() >= deadline or len(candidates) >= CONFLICT_RECOVERY_MAX_CANDIDATES:
            break
        full_key = item["Key"]
        uri = f"s3://{bucket}/{full_key}"
        if uri in seen_uris:
            continue
        if full_key in stable_full_keys:
            try:
                body = s3.get_object(Bucket=bucket, Key=full_key)["Body"].read()
            except Exception as exc:
                if _s3_error_code(exc) in ("NoSuchKey", "404", "NotFound"):
                    continue
                raise
            seen_uris.add(uri)
            candidates.append({
                "kind": kind,
                "storageUri": uri,
                "filename": filename,
                "contentType": content_type,
                "sizeBytes": len(body),
                "checksum": f"sha256:{hashlib.sha256(body).hexdigest()}",
            })
            if first_only or len(candidates) >= CONFLICT_RECOVERY_MAX_CANDIDATES:
                return candidates
            continue
        if probes >= STAGING_SCAN_MAX_PROBES:
            break
        probes += 1
        sha256_hex = None
        meta_source_hash = None
        body = None
        if hasattr(s3, "head_object"):
            try:
                head = s3.head_object(Bucket=bucket, Key=full_key)
                if isinstance(head, dict):
                    meta = head.get("Metadata") or {}
                    sha256_hex = meta.get("sha256")
                    meta_source_hash = meta.get("source_hash")
            except Exception as exc:
                if _s3_error_code(exc) in ("NoSuchKey", "404", "NotFound"):
                    continue
                raise
        if not sha256_hex:
            try:
                body = s3.get_object(Bucket=bucket, Key=full_key)["Body"].read()
                sha256_hex = hashlib.sha256(body).hexdigest()
            except Exception as exc:
                if _s3_error_code(exc) in ("NoSuchKey", "404", "NotFound"):
                    continue
                raise
        matches_source = False
        if meta_source_hash in hashes:
            source_rel = _stable_transfer_key(
                route, message_id, kind, filename, meta_source_hash)
            matches_source = _safe_staging_key(f"{STAGING_PREFIX}{source_rel}") == full_key
        matches_bytes = False
        if sha_hex and sha256_hex == sha_hex:
            rel = _email_transfer_key_for_digest(
                route, message_id, kind, 0, filename,
                content_type, f"sha256:{sha256_hex}", sha256_hex)
            matches_bytes = _safe_staging_key(f"{STAGING_PREFIX}{rel}") == full_key
        if matches_source or matches_bytes:
            if body is None:
                try:
                    body = s3.get_object(Bucket=bucket, Key=full_key)["Body"].read()
                except Exception as exc:
                    if _s3_error_code(exc) in ("NoSuchKey", "404", "NotFound"):
                        continue
                    raise
            seen_uris.add(uri)
            candidates.append({
                "kind": kind,
                "storageUri": uri,
                "filename": filename,
                "contentType": content_type,
                "sizeBytes": len(body),
                "checksum": f"sha256:{hashlib.sha256(body).hexdigest()}",
            })
            if first_only or len(candidates) >= CONFLICT_RECOVERY_MAX_CANDIDATES:
                return candidates
    return candidates


def _recover_rendered_conflict(url, body, action, event, *, steps=None, headers=None, timeout=15):
    """On a 409 for a rendered email PDF, reconcile against staged candidates
    in DATAOPS_EMAIL_DOCUMENTS_BUCKET that provably represent the same
    message/document (same route, message_id, kind, and content hash).

    Legacy byte-keyed candidates without source-hash metadata are excluded:
    the same message ID and filename do not prove that a PDF belongs to the
    current email body. An unresolved 409 remains a source conflict instead
    of re-submitting an unverifiable prior artifact.
    """
    if _is_leads_action(action) or not isinstance(event, dict) or event.get("connector") == "dropbox":
        return None
    if not isinstance(body, dict):
        return None
    data = event.get("data") if isinstance(event.get("data"), dict) else {}
    source_data = (data.get("source_event", {}).get("data", data)
                   if isinstance(data.get("source_event"), dict) else data)
    if not isinstance(source_data, dict):
        source_data = {}
    document_step = str(action.get("document_step") or "").strip()
    if document_step:
        node = (steps or {}).get(document_step)
        step_out = node.get("output") if isinstance(node, dict) and isinstance(node.get("output"), dict) else {}
        if not step_out.get("job_id"):
            return None
    else:
        out = data.get("output") if isinstance(data.get("output"), dict) else {}
        if not (out.get("bucket") and out.get("key")):
            return None
    docs = body.get("documents") if isinstance(body.get("documents"), list) else []
    if len(docs) != 1 or not isinstance(docs[0], dict):
        return None
    current = docs[0]
    kind = str(current.get("kind") or "rendered-email-pdf")
    route = str(body.get("recipientRoute") or data.get("route") or source_data.get("route") or "")
    message_id = str(body.get("messageId") or data.get("message_id") or source_data.get("message_id") or "")
    filename = str(current.get("filename") or action.get("filename") or "invoice-email.pdf")
    content_type = str(current.get("contentType") or "application/pdf")
    body_sha256 = str(data.get("checksum") or current.get("checksum") or "")
    candidates = _prior_staged_rendered(
        route, message_id, filename, content_type,
        source_data, source_hash=_source_hash(source_data),
        body_sha256=body_sha256, kind=kind, first_only=False)
    deadline = time.monotonic() + CONFLICT_RECOVERY_TIMEOUT_SECONDS
    attempts = 0
    for cand in candidates:
        if time.monotonic() >= deadline or attempts >= CONFLICT_RECOVERY_MAX_CANDIDATES:
            break
        if cand.get("kind") != kind or cand == current or cand.get("storageUri") == current.get("storageUri"):
            continue
        attempts += 1
        try:
            return base._json_request(
                url, {**body, "documents": [cand]},
                headers=headers, timeout=timeout,
            )
        except urllib.error.HTTPError as retry_exc:
            if retry_exc.code == 409:
                continue
            raise
    return None


def _email_intake_body(action, event, *, steps=None):
    data = event.get("data", {})
    source_data = data.get("source_event", {}).get("data", data)
    message_id = data.get("message_id") or source_data["message_id"]
    route = data.get("route") or source_data["route"]
    document_step = str(action.get("document_step") or "").strip()
    if document_step:
        # A prior step (merge_pdfs or render_html_to_pdf) already decided the
        # one document this email is about: forward exactly that.
        documents = [_step_document(document_step, route, message_id, action, steps,
                                    source_hash=_source_hash(source_data),
                                    source_data=source_data)]
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
            filename = base._safe_filename(action.get("filename", "invoice-email.pdf"))
            content_type = data.get("content_type", "application/pdf")
            sh = _source_hash(source_data)
            prior = _prior_staged_rendered(
                route, message_id, filename, content_type,
                source_data, source_hash=sh,
                body_sha256=data.get("checksum"),
                kind="rendered-email-pdf", first_only=True)
            if prior:
                documents.append(prior[0])
            else:
                key = _stable_transfer_key(route, message_id, "rendered-email-pdf", filename, sh)
                body = base._s3_body(output)
                checksum = f"sha256:{hashlib.sha256(body).hexdigest()}"
                storage_uri, body, checksum = _stage_document_first_write(
                    key, body, content_type, checksum, sh)
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


RENDER_WAIT_SECONDS = 180  # cold render starts took 52.6 s; WorkerFunction Timeout covers this
RENDER_POLL_SECONDS = 3
RENDER_POLL_MAX_SECONDS = 15


def _await_document(document, job_id, ref):
    """Read a prior step's stored file. A render step only queues its job, so
    the PDF may not exist yet: wait a bounded time, then fail with an error
    naming the render job instead of a raw NoSuchKey."""
    deadline = time.monotonic() + (RENDER_WAIT_SECONDS if job_id else 0)
    delay = RENDER_POLL_SECONDS
    while True:
        try:
            return base._s3_body(document)
        except Exception as exc:
            code = getattr(exc, "response", {}).get("Error", {}).get("Code")
            if code not in ("NoSuchKey", "404"):
                raise
            if time.monotonic() >= deadline:
                what = f"render job {job_id}" if job_id else f"step '{ref}'"
                raise RuntimeError(
                    f"dataops document_step '{ref}': output s3://{document['bucket']}/{document['key']} "
                    f"does not exist ({what} has not produced it; the job may have failed - "
                    f"check the render dead-letter queue)") from None
            time.sleep(min(delay, max(deadline - time.monotonic(), 0)))
            delay = min(delay * 1.5, RENDER_POLL_MAX_SECONDS)


def _step_document(ref, route, message_id, action, steps, source_hash=None, source_data=None):
    """Stage the one document a prior step produced (a merge_pdfs or
    render_html_to_pdf output: ``{"filename", "output": {"bucket", "key"}}``
    under the step's output) and build its intake document entry. The
    checksum is computed over the stored bytes, so passthrough documents keep
    the original attachment's."""
    node = (steps or {}).get(ref)
    output = node.get("output") if isinstance(node, dict) and isinstance(node.get("output"), dict) else {}
    document = output.get("output") if isinstance(output.get("output"), dict) else {}
    if not (isinstance(document, dict) and document.get("bucket") and document.get("key")):
        raise ValueError(f"dataops document_step '{ref}' does not reference a stored file")
    filename = base._safe_filename(str(document.get("filename") or output.get("filename") or "").strip()
                                   or action.get("filename") or "invoice-document.pdf")
    kind = "rendered-email-pdf" if output.get("job_id") else "attachment"
    key = None
    if output.get("job_id") and source_hash:
        # Render-produced PDFs differ byte-wise between runs: key them by
        # the email identity and reuse the first staged copy on replay.
        prior = _prior_staged_rendered(
            route, message_id, filename, "application/pdf",
            source_data, source_hash=source_hash,
            body_sha256=output.get("checksum") or document.get("checksum"),
            kind=kind, first_only=True)
        if prior:
            return prior[0]
        key = _stable_transfer_key(route, message_id, kind, filename, source_hash)
    body = _await_document(document, output.get("job_id"), ref)
    checksum = f"sha256:{hashlib.sha256(body).hexdigest()}"
    if key:
        storage_uri, body, checksum = _stage_document_first_write(
            key, body, "application/pdf", checksum, source_hash)
    else:
        storage_uri = _stage_document(
            _email_transfer_key(route, message_id, kind, 0, filename,
                                body, "application/pdf", checksum),
            body, "application/pdf", checksum)
    return {
        "kind": kind,
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
