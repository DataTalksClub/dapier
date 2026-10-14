"""Per-connector ingress normalization: raw payloads become dapier events.

Each connector that owns an ingress (the SES catch-all's inbound-email
contract, the html-renderer's completion schema) registers a normalizer
here; ``normalize_event`` tries them in registration order and passes
unrecognized payloads through unchanged (they may already be normalized
events, e.g. published through the generic hook path). The SQS envelope
itself is unwrapped by the worker before this runs.
"""

NORMALIZERS = []
EVENT_IDENTITIES = {}


def event_identity(connector, event):
    """Register a plugin's stable processing identity across ingress paths."""
    def register(identity):
        EVENT_IDENTITIES[(connector, event)] = identity
        return identity
    return register


def processing_id(payload):
    """Stable step identity; explicit operator replays keep their fresh id."""
    delivery_id = payload["id"]
    if str(delivery_id).startswith(("replay-", "inbox-replay-")):
        return delivery_id
    identity = EVENT_IDENTITIES.get((payload.get("connector"), payload.get("event")))
    return (identity(payload) if identity else None) or delivery_id


def ingress(normalize):
    """Register ``normalize(payload) -> event | None`` for one contract."""
    NORMALIZERS.append(normalize)
    return normalize


def normalize_event(payload):
    for normalize in NORMALIZERS:
        event = normalize(payload)
        if event is not None:
            return event
    return payload


def _route_label(route):
    """The route label workflows key on: the receiving address's local part.

    Datamailer published bare labels ("newsletter"); Relay publishes the
    full receiving address, so strip the domain to land on the same label.
    """
    name = str(route or "").strip().lower()
    if "@" in name:
        name = name.split("@", 1)[0].strip()
    return name


def _s3():
    import boto3

    return boto3.client("s3")


def _content_value(text, content_type):
    """Datamailer's inline body-part shape for a decoded part (or None)."""
    if not text:
        return None
    return {"content_type": content_type, "value": text, "size": len(text.encode())}


def _stage_attachments(message, event_id):
    """Stage raw attachment parts into the dapier mail bucket.

    Each staged attachment mirrors the Datamailer attachment dict — the
    invoice flows build document keys from the checksum — so document
    actions can fetch their bytes by s3 reference exactly as before.
    Best-effort: with MAIL_BUCKET unset, or a write failing, the
    attachment list comes back empty and the event still runs.
    """
    import hashlib
    import os
    import re

    bucket = os.environ.get("MAIL_BUCKET")
    if not bucket:
        return []
    client = _s3()
    attachments = []
    for index, part in enumerate(message.walk()):
        if part.is_multipart():
            continue
        disposition = part.get_content_disposition() or ""
        filename = part.get_filename() or ""
        if disposition != "attachment" and not filename:
            continue
        payload = part.get_payload(decode=True)
        if payload is None:
            continue
        safe_name = re.sub(r"[^A-Za-z0-9._-]", "_", filename or f"attachment-{index + 1}")
        object_key = f"relay-attachments/{event_id}/attachments/{index + 1:03d}-{safe_name}"
        try:
            client.put_object(
                Bucket=bucket, Key=object_key, Body=payload,
                ContentType=part.get_content_type(),
                ServerSideEncryption="AES256",
            )
        except Exception:
            continue
        attachments.append({
            "filename": filename or None,
            "content_type": part.get_content_type(),
            "content_id": str(part.get("content-id") or "").strip("<>"),
            "disposition": disposition or "inline",
            "size": len(payload),
            "checksum": f"sha256:{hashlib.sha256(payload).hexdigest()}",
            "s3": {"bucket": bucket, "key": object_key},
        })
    return attachments


def _rehydrate_raw(payload):
    """Rebuild the rich event data a slim (Relay) event omits.

    Relay's inbound-email v1 carries no body or attachments — only the
    raw_mime reference — so parse the raw email here: the body rides
    inline (capped like the intake envelope) and attachments are staged
    into the dapier mail bucket. Best-effort: any failure returns None
    and the event runs with what it has.
    """
    pointer = payload.get("raw_mime")
    if not (isinstance(pointer, dict) and pointer.get("bucket") and pointer.get("key")):
        return None
    try:
        from ..triggers.intake import email_ingress

        raw = _s3().get_object(Bucket=pointer["bucket"], Key=pointer["key"])["Body"].read()
        message = email_ingress.parse_message(raw)
        body = {
            "text": _content_value(email_ingress._body(message, "plain"), "text/plain"),
            "html": _content_value(email_ingress._body(message, "html"), "text/html"),
        }
        attachments = _stage_attachments(message, payload.get("event_id"))
    except Exception:
        return None
    return {"body": body, "attachments": attachments}

@ingress
def _normalize_email(payload):
    if payload.get("contract") != "inbound-email":
        return None
    if payload.get("version") != 1:
        raise ValueError(f"unsupported inbound-email contract version: {payload.get('version')}")
    body = payload.get("body")
    attachments = payload.get("attachments")
    if body is None or attachments is None:
        full = _rehydrate_raw(payload)
        if full is not None:
            if body is None:
                body = full.get("body")
            if attachments is None:
                attachments = full.get("attachments")
    recipients = payload.get("recipients")
    if isinstance(recipients, dict) and "matched" not in recipients:
        recipients = dict(recipients, matched=recipients.get("addresses") or [])
    route = payload.get("route")
    label = _route_label(route)
    data = {
        "route": label,
        "route_address": route,
        "message_id": payload.get("message_id"),
        "sender": payload.get("sender"),
        "recipients": recipients,
        "subject": payload.get("subject"),
        "date": payload.get("date") or payload.get("occurred_at"),
        "body": body,
        "html": body.get("html") if isinstance(body, dict) else None,
        "attachments": attachments or [],
        "raw_mime": payload.get("raw_mime"),
    }
    if payload.get("inbound_message_id") is not None:
        data["inbound_message_id"] = payload["inbound_message_id"]
    return {
        "schema_version": "1.0",
        "id": payload["event_id"],
        "correlation_id": payload["event_id"],
        "connector": "email",
        "event": "message.received",
        "source": label,
        "occurred_at": payload.get("occurred_at"),
        "data": data,
    }


@ingress
def _normalize_renderer(payload):
    if payload.get("schema") != "html-renderer.completed.v1":
        return None
    source = payload.get("context", {}).get("source_event", {})
    return {
        "schema_version": "1.0",
        "id": payload["job_id"],
        "correlation_id": source.get("correlation_id", payload["job_id"]),
        "connector": "renderer",
        "event": "job.completed",
        "source": "html-renderer",
        "occurred_at": payload["timestamp"],
        "data": {
            "job_id": payload["job_id"],
            "output": payload["output"],
            "content_type": payload.get("content_type"),
            "size_bytes": payload.get("size_bytes"),
            "checksum": payload.get("checksum"),
            "source_event": source,
            "message_id": source.get("data", {}).get("message_id"),
            "route": source.get("data", {}).get("route"),
        },
    }
