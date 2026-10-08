"""The trigger connector catalog: one chip per source the palette suggests.

These feed the designer's Triggers group and the event suggestions per
connector. Ingress normalization for the sources dapier itself receives
lives in ``connectors.ingress``.

The connectors without a module of their own (renderer, custom) register
their trigger sample discoveries here, next to the chip that offers them;
provider connectors sample in their own plugin modules — zoom one
documented payload per declared event, slack and youtube a live fetch with
the history/synthetic fallback, mailchimp one payload per Mailchimp webhook
type — and schedule and poll in connectors.schedule / connectors.poll — a
schedule fire is synthesized and a poll fire has a live fetch path, so
each owns its sample.
"""
from .registry import Connector, connector

connector(Connector(name="email", label="Email",
                    events=("message.received", "bounce.received", "complaint.received"), icon="mail",
                    event_info={
                        "message.received": ("Email arrives", "A message reaches one of this workflow's Dapier addresses"),
                        "bounce.received": ("Email bounced", "A message Dapier sent could not be delivered"),
                        "complaint.received": ("Spam complaint", "A recipient marked a message Dapier sent as spam"),
                    }))
connector(Connector(name="renderer" , label="Renderer", events=("job.completed",), icon="file-text",
                    event_info={"job.completed": ("Render finished", "A renderer job finished and its output is ready")}))
connector(Connector(name="schedule", label="Schedule", events=("schedule.triggered",), icon="clock",
                    event_info={"schedule.triggered": ("On schedule", "The workflow's cron or rate schedule fires")}))
connector(Connector(name="poll", label="Poll", events=("item.new",), icon="refresh-cw",
                    event_info={"item.new": ("New item", "A polled API returns an item not seen before")}))
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
                              "route": "todo"}},
    "message_id": "<discover-00000000@dtcdev.click>",
    "route": "todo",
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
