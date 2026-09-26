"""Per-connector ingress normalization: raw payloads become dapier events.

Each connector that owns an ingress (the SES catch-all's inbound-email
contract, the html-renderer's completion schema) registers a normalizer
here; ``normalize_event`` tries them in registration order and passes
unrecognized payloads through unchanged (they may already be normalized
events, e.g. published through the generic hook path). The SQS envelope
itself is unwrapped by the worker before this runs.
"""

NORMALIZERS = []


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


@ingress
def _normalize_email(payload):
    if payload.get("contract") != "inbound-email":
        return None
    if payload.get("version") != 1:
        raise ValueError(f"unsupported inbound-email contract version: {payload.get('version')}")
    return {
        "schema_version": "1.0",
        "id": payload["event_id"],
        "correlation_id": payload["event_id"],
        "connector": "email",
        "event": "message.received",
        "source": payload["route"],
        "occurred_at": payload["occurred_at"],
        "data": {
            "route": payload["route"],
            "message_id": payload["message_id"],
            "sender": payload["sender"],
            "recipients": payload["recipients"],
            "subject": payload["subject"],
            "date": payload.get("date") or payload["occurred_at"],
            "body": payload["body"],
            "html": payload["body"].get("html"),
            "attachments": payload["attachments"],
            "raw_mime": payload["raw_mime"],
        },
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
