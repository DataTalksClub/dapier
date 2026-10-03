"""Zoom's documented per-event webhook samples behind the sample pull."""
from src.dapier.connectors.trigger_discovery import per_event_sample_fetch

# --- trigger samples: one documented payload per declared event -----------------
#
# The events here are exactly the ones triggers.intake.zoom_webhooks accepts
# and the Zoom chip declares; each sample mirrors what a real delivery
# publishes, metadata only (download tokens and participant payloads stay
# out of runs). The event field of a discovery request picks the payload.

_ZOOM_EVENT_SAMPLES = {
    "recording.completed": {
        "account_id": "discover-account",
        "meeting_id": "94839610293",
        "meeting_uuid": "discover-sample-uuid",
        "topic": "Weekly sync",
        "host_id": "discover-host",
        "host_email": "host@example.test",
        "start_time": "2026-09-27T10:00:00Z",
        "share_url": "https://example.zoom.us/rec/share/discover-sample",
        "video_files": [{
            "id": "discover-mp4",
            "file_type": "MP4",
            "recording_type": "cloud_recording",
            "file_size": 184357376,
            "play_url": "https://example.zoom.us/rec/play/discover-sample",
            "download_url": "https://example.zoom.us/rec/download/discover-sample",
        }],
    },
    "recording.transcript_completed": {
        "account_id": "discover-account",
        "meeting_id": "94839610293",
        "meeting_uuid": "discover-sample-uuid",
        "topic": "Weekly sync",
        "host_id": "discover-host",
        "host_email": "host@example.test",
        "start_time": "2026-09-27T10:00:00Z",
        "share_url": "https://example.zoom.us/rec/share/discover-sample",
        "video_files": [
            {
                "id": "discover-mp4",
                "file_type": "MP4",
                "recording_type": "cloud_recording",
                "file_size": 184357376,
                "play_url": "https://example.zoom.us/rec/play/discover-sample",
                "download_url": "https://example.zoom.us/rec/download/discover-sample",
            },
            {
                "id": "discover-vtt",
                "file_type": "TRANSCRIPT",
                "recording_type": "audio_transcript",
                "file_size": 18234,
                "play_url": "https://example.zoom.us/rec/play/discover-vtt",
                "download_url": "https://example.zoom.us/rec/download/discover-vtt",
            },
        ],
    },
    "meeting.started": {
        "account_id": "discover-account",
        "uuid": "discover-sample-uuid",
        "id": "94839610293",
        "topic": "Weekly sync",
        "host_id": "discover-host",
        "start_time": "2026-09-27T10:00:00Z",
        "duration": 60,
        "timezone": "Europe/Berlin",
    },
    "meeting.ended": {
        "account_id": "discover-account",
        "uuid": "discover-sample-uuid",
        "id": "94839610293",
        "topic": "Weekly sync",
        "host_id": "discover-host",
        "start_time": "2026-09-27T10:00:00Z",
        "end_time": "2026-09-27T11:02:00Z",
        "duration": 60,
        "timezone": "Europe/Berlin",
    },
    # The shape zoom_webhooks.registration_data publishes: the meeting's
    # identity plus the registrant's own submitted fields (the event's
    # payload); join URLs and meeting settings stay out.
    "meeting.registration_created": {
        "account_id": "discover-account",
        "meeting_id": "94839610293",
        "meeting_uuid": "discover-sample-uuid",
        "topic": "Weekly sync",
        "start_time": "2026-09-28T10:00:00Z",
        "timezone": "Europe/Berlin",
        "registrant_id": "discover-registrant",
        "email": "ada@example.test",
        "first_name": "Ada",
        "last_name": "Lovelace",
        "status": "approved",
    },
    # Webinar lifecycle and registration share the meeting events' shapes —
    # the intake maps them with the same builders, only the event name and
    # the object's kind differ.
    "webinar.started": {
        "account_id": "discover-account",
        "uuid": "discover-sample-uuid",
        "id": "98765432100",
        "topic": "Product webinar",
        "host_id": "discover-host",
        "start_time": "2026-09-27T10:00:00Z",
        "duration": 60,
        "timezone": "Europe/Berlin",
    },
    "webinar.ended": {
        "account_id": "discover-account",
        "uuid": "discover-sample-uuid",
        "id": "98765432100",
        "topic": "Product webinar",
        "host_id": "discover-host",
        "start_time": "2026-09-27T10:00:00Z",
        "end_time": "2026-09-27T11:02:00Z",
        "duration": 60,
        "timezone": "Europe/Berlin",
    },
    "webinar.registration_created": {
        "account_id": "discover-account",
        "meeting_id": "98765432100",
        "meeting_uuid": "discover-sample-uuid",
        "topic": "Product webinar",
        "start_time": "2026-09-28T10:00:00Z",
        "timezone": "Europe/Berlin",
        "registrant_id": "discover-registrant",
        "email": "ada@example.test",
        "first_name": "Ada",
        "last_name": "Lovelace",
        "status": "approved",
    },
}

_PER_EVENT_ZOOM_SAMPLE = per_event_sample_fetch(
    "zoom", "recording.completed", _ZOOM_EVENT_SAMPLES)


