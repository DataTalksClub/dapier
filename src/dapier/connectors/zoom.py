"""Zoom connector: meetings and recordings discovery, plus the health check.

Zoom connections come in two flavors: OAuth grants (refreshable tokens,
verified against ``users/me``) and webhook-only setups that store just a
signing secret. Both runners delegate to the shared provider layer; a
webhook-only connection reports a failed check with the refresh error
instead of pretending to be healthy.
"""
from ..connections import discovery as provider
from ..engine.actions.zoom import run_zoom_find_meeting, run_zoom_find_recording
from .registry import (
    Action,
    ConnectionTest,
    Discovery,
    register,
    register_connection_test,
    register_discovery,
)

register(Action(
    type="zoom_find_meeting",
    label="Zoom",
    description="Find a Zoom meeting by id, or by topic among upcoming meetings",
    icon="zoom",
    run=lambda action, event, workflow_id, steps=None: run_zoom_find_meeting(
        action, event, steps=steps),
    required=frozenset({"connection_id"}),
    optional=frozenset({"meeting_id", "topic", "match"}),
    fields=(
        {"key": "connection_id", "label": "Connection ID", "placeholder": "zoom",
         "required": True},
        {"key": "meeting_id", "label": "Meeting ID",
         "discover": {"resource": "zoom.meetings"}},
        {"key": "topic", "label": "Topic",
         "help": "Used when no meeting id is given; searched across upcoming meetings"},
        {"key": "match", "label": "Topic match", "type": "select",
         "options": ["contains", "exact"], "default": "contains"},
    ),
))

register(Action(
    type="zoom_find_recording",
    label="Zoom: find recording",
    description="Find Zoom cloud recordings by meeting id or topic, or the most recent (Find Recording)",
    icon="zoom",
    run=lambda action, event, workflow_id, steps=None: run_zoom_find_recording(
        action, event, steps=steps),
    required=frozenset({"connection_id"}),
    optional=frozenset({"meeting_id", "topic", "match"}),
    fields=(
        {"key": "connection_id", "label": "Connection ID", "placeholder": "zoom",
         "required": True},
        {"key": "meeting_id", "label": "Meeting ID",
         "discover": {"resource": "zoom.recordings"}},
        {"key": "topic", "label": "Topic",
         "help": "Filters the last 30 days of recordings when no meeting id is given; with neither, the most recent recording wins"},
        {"key": "match", "label": "Topic match", "type": "select",
         "options": ["contains", "exact"], "default": "contains"},
    ),
))


def _listed(resource):
    """A discovery runner that lists ``resource`` through the shared layer."""
    def run(connection, params, *, transport=None):
        return provider.discover(connection, resource, params, transport=transport)
    return run


def _tested(connection):
    return provider.test_connection(connection)


register_discovery(Discovery(
    name="meetings",
    connector="zoom",
    label="Meetings",
    description="Upcoming meetings on the account",
    run=_listed("meetings"),
))

register_discovery(Discovery(
    name="recordings",
    connector="zoom",
    label="Recordings",
    description="Cloud recordings from the last 30 days",
    run=_listed("recordings"),
))

register_connection_test(ConnectionTest(connector="zoom", run=_tested))


# --- trigger discovery: meeting options for the find action's id field ----------

from . import trigger_discovery  # noqa: E402
from .trigger_discovery import (  # noqa: E402
    DEFAULT_LIMIT,
    TriggerDiscovery,
    per_event_sample_fetch,
    register_trigger_discovery,
)


def _fetch_meeting_options(event=None, connection_id=None, limit=DEFAULT_LIMIT):
    """Meeting options via the registry listing (first connected Zoom
    connection when no id is named)."""
    return trigger_discovery.options_from_registry(
        "zoom.meetings", connection_id, limit, provider="zoom",
        option_of=lambda item: {"value": item.get("id"),
                                "label": item.get("name") or item.get("id")})


register_trigger_discovery(TriggerDiscovery(
    connector="zoom", label="Zoom", kind="options", resource="zoom.meetings",
    fetch=_fetch_meeting_options))


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
}

register_trigger_discovery(TriggerDiscovery(
    connector="zoom", label="Zoom", kind="sample", resource="",
    fetch=per_event_sample_fetch("zoom", "recording.completed", _ZOOM_EVENT_SAMPLES)))
