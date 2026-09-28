"""The trigger connector catalog: one chip per source the palette suggests.

These feed the designer's Triggers group and the event suggestions per
connector. Ingress normalization for the sources dapier itself receives
lives in ``connectors.ingress``.

The connectors without a module of their own (renderer, custom) register
their trigger sample discoveries here, next to the chip that offers them;
connectors with a module sample in their own module — zoom one documented
payload per declared event, slack and youtube a live fetch with the
history/synthetic fallback, mailchimp one payload per Mailchimp webhook
type — and schedule and poll in connectors.schedule / connectors.poll — a
schedule fire is synthesized and a poll fire has a live fetch path, so
each owns its sample.
"""
from .registry import Connector, connector

connector(Connector(name="email", label="Email",
                    events=("message.received", "bounce.received", "complaint.received"), icon="mail"))
connector(Connector(name="youtube", label="YouTube", events=("video.published",), icon="youtube"))
connector(Connector(name="dropbox", label="Dropbox", events=("file.created", "file.updated", "file.deleted"), icon="dropbox"))
connector(Connector(name="zoom", label="Zoom",
                    events=("recording.completed", "recording.transcript_completed",
                            "meeting.started", "meeting.ended",
                            "meeting.registration_created",
                            "webinar.started", "webinar.ended",
                            "webinar.registration_created"),
                    icon="video"))
connector(Connector(name="slack", label="Slack",
                    events=("message.received", "app.mention",
                            "reaction.added", "member.joined"),
                    icon="slack"))
connector(Connector(name="telegram", label="Telegram",
                    events=("message.received", "channel_post.received",
                            "callback_query.received"),
                    icon="send"))
connector(Connector(name="mailchimp", label="Mailchimp",
                    events=("subscribe", "unsubscribe", "profile", "upemail",
                            "cleaned", "campaign", "member.new"),
                    icon="mail"))
# Provider chips whose fires come from poll sources (triggers/poll_sources):
# a stored poll trigger with a non-http source publishes these connectors'
# events, scoped per trigger through the poll-name filter. The zoom chip
# above joins them on recording.completed: the zoom.recordings poll source
# (connectors/zoom.py) publishes it on a schedule, no Zoom app required.
connector(Connector(name="google-sheets", label="Google Sheets", events=("row.new", "row.updated"), icon="table"))
connector(Connector(name="google-drive", label="Google Drive",
                    events=("file.created", "file.updated", "file.deleted"),
                    icon="folder"))
connector(Connector(name="google-calendar", label="Google Calendar",
                    events=("event.new",),
                    icon="calendar"))
connector(Connector(name="s3", label="S3",
                    events=("file.created", "file.updated", "file.deleted"),
                    icon="database"))
connector(Connector(name="rss", label="RSS", events=("item.new",), icon="rss"))
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

# The Custom chip is freeform: a placeholder the author pastes over, like
# Zapier's "copy in sample data". The event name is the one the /hooks/custom
# ingress actually publishes (api.router: connector "custom", event
# "received"), so a filter copied from the sample matches a real delivery.
register_trigger_discovery(TriggerDiscovery(
    connector="custom", label="Custom", kind="sample", resource="",
    fetch=history_or_synthetic_fetch("custom", "received", {
        "note": "Replace this object with your own sample payload",
    })))
