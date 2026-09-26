"""Verify Zoom cloud-recording webhooks and normalize safe workflow events."""

import hashlib
import hmac
import json
import time

from ...connections import credentials
from ...connections import records


def verify(headers, body, secret, *, now=None):
    """Zoom signs v0:<timestamp>:<raw body>; reject stale deliveries."""
    normalized = {str(key).lower(): str(value) for key, value in (headers or {}).items()}
    timestamp = normalized.get("x-zm-request-timestamp", "")
    signature = normalized.get("x-zm-signature", "")
    try:
        sent = int(timestamp)
    except (TypeError, ValueError):
        return False
    if abs(int(now if now is not None else time.time()) - sent) > 300:
        return False
    expected = "v0=" + hmac.new(secret.encode(), b"v0:" + timestamp.encode() + b":" + body,
                                hashlib.sha256).hexdigest()
    return hmac.compare_digest(signature, expected)


def recording_data(payload):
    """Return metadata only; Zoom download tokens and private payloads stay out of runs."""
    if not isinstance(payload, dict):
        return None
    meeting = payload.get("object")
    if not isinstance(meeting, dict):
        return None
    files = meeting.get("recording_files") or []
    if not isinstance(files, list):
        return None
    videos = [
        {
            "id": item.get("id"),
            "file_type": item.get("file_type"),
            "recording_type": item.get("recording_type"),
            "file_size": item.get("file_size"),
            "play_url": item.get("play_url"),
            "download_url": item.get("download_url"),
        }
        for item in files if isinstance(item, dict)
        and str(item.get("file_type", "")).upper() in {"MP4", "M4V"}
    ]
    if not videos:
        return None
    return {
        "account_id": payload.get("account_id"),
        "meeting_id": meeting.get("id"),
        "meeting_uuid": meeting.get("uuid"),
        "topic": meeting.get("topic"),
        "host_id": meeting.get("host_id"),
        "host_email": meeting.get("host_email"),
        "start_time": meeting.get("start_time"),
        "share_url": meeting.get("share_url"),
        "video_files": videos,
    }


def handle(connection_id, headers, body, *, connections_table, publish, now=None):
    """Return (HTTP status, JSON body) for a connection-specific Zoom URL."""
    try:
        connection_id = records.validate_connection_id(connection_id)
    except records.ConnectionError:
        return 404, {"error": "not found"}
    connection = records.get_connection(connections_table, connection_id)
    if not connection or connection.get("provider") != "zoom" or connection.get("status") == "revoked":
        return 404, {"error": "not found"}
    try:
        secret = credentials.get_credential(connection["credential_id"])["webhook_secret"]
    except (KeyError, TypeError):
        return 503, {"error": "Zoom webhook is not configured"}
    if not verify(headers, body, secret, now=now):
        return 401, {"error": "invalid signature"}
    try:
        message = json.loads(body)
    except (ValueError, UnicodeDecodeError):
        return 400, {"error": "invalid JSON"}
    if not isinstance(message, dict):
        return 400, {"error": "invalid Zoom event"}
    event = message.get("event")
    if event == "endpoint.url_validation":
        challenge = message.get("payload")
        plain = challenge.get("plainToken") if isinstance(challenge, dict) else None
        if not isinstance(plain, str) or not plain or len(plain) > 512:
            return 400, {"error": "invalid validation token"}
        if connection.get("status") != records.STATUS_CONNECTED:
            updated = dict(connection)
            updated.update(status=records.STATUS_CONNECTED, account_title="Zoom webhook",
                           connected_at=records.now_iso(), updated_at=records.now_iso(),
                           version=int(connection.get("version", 0)) + 1)
            records.put_connection(connections_table, updated)
        return 200, {"plainToken": plain, "encryptedToken": hmac.new(
            secret.encode(), plain.encode(), hashlib.sha256).hexdigest()}
    if event != "recording.completed":
        return 200, {"accepted": False}
    data = recording_data(message.get("payload"))
    if data is None:
        return 200, {"accepted": False}
    account_id = data.get("account_id")
    if not account_id or not data.get("meeting_uuid") or not message.get("event_ts"):
        return 400, {"error": "incomplete Zoom recording event"}
    try:
        records.check_binding(connection, account_id)
    except records.BindingError:
        return 403, {"error": "Zoom account does not match connection"}
    if not connection.get("verified_account_id"):
        updated = records.mark_connected(
            connection, verified_account_id=account_id,
            account_title=account_id, granted_scopes=[], connected_by="zoom-webhook")
        records.put_connection(connections_table, updated)
    identity = f"{connection_id}:{data.get('meeting_uuid') or data.get('meeting_id')}:{message.get('event_ts')}"
    event_id = "zoom:" + hashlib.sha256(identity.encode()).hexdigest()[:32]
    publish("zoom", "recording.completed", {"connection_id": connection_id, **data},
            source=connection_id, event_id=event_id)
    return 200, {"accepted": True}
