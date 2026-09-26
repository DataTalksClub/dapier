"""The trigger connector catalog: one chip per source the palette suggests.

These feed the designer's Triggers group and the event suggestions per
connector. Ingress normalization for the sources dapier itself receives
lives in ``connectors.ingress``.
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
