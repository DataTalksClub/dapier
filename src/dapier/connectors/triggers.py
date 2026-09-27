"""The trigger connector catalog: one chip per source the palette suggests.

These feed the designer's Triggers group and the event suggestions per
connector. Ingress normalization for the sources dapier itself receives
lives in ``connectors.ingress``.

The connectors without a module of their own (renderer, custom) register
their trigger sample discoveries here, next to the chip that offers them;
youtube and zoom sample in their connector modules, and schedule and poll
in connectors.schedule / connectors.poll — a schedule fire is synthesized
and a poll fire has a live fetch path, so each owns its sample.
"""
from .registry import Connector, connector

connector(Connector(name="email", label="Email", events=("message.received",), icon="mail"))
connector(Connector(name="youtube", label="YouTube", events=("video.published",), icon="youtube"))
connector(Connector(name="dropbox", label="Dropbox", events=("file.created",), icon="dropbox"))
connector(Connector(name="zoom", label="Zoom", events=("recording.completed",), icon="video"))
connector(Connector(name="renderer", label="Renderer", events=("job.completed",), icon="file-text"))
connector(Connector(name="schedule", label="Schedule", events=("schedule.triggered",), icon="clock"))
connector(Connector(name="poll", label="Poll", events=("item.new",), icon="refresh-cw"))
connector(Connector(name="custom", label="Custom", events=(), icon="webhook"))


# --- trigger discovery: samples for the sources without a connector module ---

from .trigger_discovery import (  # noqa: E402
    TriggerDiscovery,
    history_or_synthetic_fetch,
    register_trigger_discovery,
)

# The html-renderer completion contract (see connectors.ingress._normalize_renderer).
_RENDERER_SYNTHETIC_DATA = {
    "job_id": "render-4f2a1b",
    "output": {"bucket": "dapier-renders", "key": "invoices/4137.pdf"},
    "content_type": "application/pdf",
    "size_bytes": 81244,
    "checksum": "sha256:1f3ac2",
    "source_event": {"connector": "email", "event": "message.received",
                     "data": {"subject": "Invoice #4137 - September",
                              "route": "todo@dtcdev.click"}},
    "message_id": "<discover-00000000@dtcdev.click>",
    "route": "todo@dtcdev.click",
}

register_trigger_discovery(TriggerDiscovery(
    connector="renderer", label="Renderer", kind="sample", resource="",
    fetch=history_or_synthetic_fetch("renderer", "job.completed", _RENDERER_SYNTHETIC_DATA)))

# The YouTube PubSubHubbub notification (see api.router._youtube).
register_trigger_discovery(TriggerDiscovery(
    connector="youtube", label="YouTube", kind="sample", resource="",
    fetch=history_or_synthetic_fetch("youtube", "video.published", {
        "video_id": "dQw4w9WgXcQ",
        "channel_id": "UCbW5IB0F8d1MpdW2AhifDzw",
        "title": "Deploying dapier: a walkthrough",
        "url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
    })))

# Zoom's recording.completed webhook, metadata only — download tokens and
# private payloads stay out of runs (see triggers.intake.zoom_webhooks).
register_trigger_discovery(TriggerDiscovery(
    connector="zoom", label="Zoom", kind="sample", resource="",
    fetch=history_or_synthetic_fetch("zoom", "recording.completed", {
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
    })))

# The Custom chip is freeform: a placeholder the author pastes over, like
# Zapier's "copy in sample data".
register_trigger_discovery(TriggerDiscovery(
    connector="custom", label="Custom", kind="sample", resource="",
    fetch=history_or_synthetic_fetch("custom", "occurred", {
        "note": "Replace this object with your own sample payload",
    })))
