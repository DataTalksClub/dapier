"""Verify Zoom webhooks and normalize safe workflow events.

The four events dapier subscribes to are the ones the Zoom chip declares
(connectors.triggers): recording.completed, recording.transcript_completed,
meeting.started, meeting.ended. Every builder returns metadata only —
download tokens, participant lists, and private payloads stay out of runs.
"""

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


RECORDING_EVENTS = ("recording.completed", "recording.transcript_completed")
MEETING_EVENTS = ("meeting.started", "meeting.ended")
# The video-file filter for recording events; transcript_completed deliveries
# widen it so the TRANSCRIPT entry rides along with the video.
RECORDING_FILE_TYPES = frozenset({"MP4", "M4V"})
TRANSCRIPT_FILE_TYPES = frozenset({"MP4", "M4V", "TRANSCRIPT"})


def recording_data(payload, *, file_types=RECORDING_FILE_TYPES):
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
        and str(item.get("file_type", "")).upper() in file_types
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


def meeting_data(payload):
    """Metadata-only flatten of a meeting lifecycle delivery (started/ended).

    Zoom nests the details under ``payload.object`` and puts ``account_id``
    at the payload level for meeting events, so the account for binding is
    read from the object with a payload-level fallback. Only the scheduling
    facts a workflow templates over survive — participant lists and
    per-account settings stay out of runs; ``end_time`` appears on ended
    deliveries only.
    """
    if not isinstance(payload, dict):
        return None
    meeting = payload.get("object")
    if not isinstance(meeting, dict):
        return None
    data = {
        "account_id": meeting.get("account_id") or payload.get("account_id"),
        "uuid": meeting.get("uuid"),
        "id": meeting.get("id"),
        "topic": meeting.get("topic"),
        "host_id": meeting.get("host_id"),
        "start_time": meeting.get("start_time"),
        "duration": meeting.get("duration"),
        "timezone": meeting.get("timezone"),
    }
    if meeting.get("end_time"):
        data["end_time"] = meeting.get("end_time")
    return data


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
    if event not in RECORDING_EVENTS + MEETING_EVENTS:
        return 200, {"accepted": False}
    if event in RECORDING_EVENTS:
        data = recording_data(message.get("payload"),
                              file_types=TRANSCRIPT_FILE_TYPES
                              if event == "recording.transcript_completed"
                              else RECORDING_FILE_TYPES)
        if data is None:
            return 200, {"accepted": False}
        account_id = data.get("account_id")
        if not account_id or not data.get("meeting_uuid") or not message.get("event_ts"):
            return 400, {"error": "incomplete Zoom recording event"}
        # recording.completed keeps its original dedup identity (no event
        # name); the newer events name themselves so one meeting can complete
        # and transcript-complete at the same timestamp without colliding.
        who = data.get("meeting_uuid") or data.get("meeting_id")
        identity = (f"{connection_id}:{who}:{message.get('event_ts')}"
                    if event == "recording.completed"
                    else f"{connection_id}:{event}:{who}:{message.get('event_ts')}")
    else:
        data = meeting_data(message.get("payload"))
        account_id = (data or {}).get("account_id")
        if (data is None or not account_id or not data.get("uuid")
                or not data.get("id") or not message.get("event_ts")):
            return 400, {"error": "incomplete Zoom meeting event"}
        identity = f"{connection_id}:{event}:{data.get('uuid')}:{message.get('event_ts')}"
    try:
        records.check_binding(connection, account_id)
    except records.BindingError:
        return 403, {"error": "Zoom account does not match connection"}
    if not connection.get("verified_account_id"):
        updated = records.mark_connected(
            connection, verified_account_id=account_id,
            account_title=account_id, granted_scopes=[], connected_by="zoom-webhook")
        records.put_connection(connections_table, updated)
    event_id = "zoom:" + hashlib.sha256(identity.encode()).hexdigest()[:32]
    publish("zoom", event, {"connection_id": connection_id, **data},
            source=connection_id, event_id=event_id)
    return 200, {"accepted": True}
