"""Google family plugin entry point: one OAuth connection backs Sheets,
Drive, Calendar, Gmail and YouTube, so the family ships as one plugin
(connection granularity = plugin granularity).

The registration lives in the :mod:`plugins.google.connector` package and
the runners in :mod:`plugins.google.runners`; importing them registers
everything, like every connector module. This file is the loader's entry
point plus the trigger chips (Gmail's chip stays next to its poll source
in :mod:`plugins.google.connector.gmail`, as before the move).
"""
from plugins.google import connector  # noqa: F401  (import = registration)
from src.dapier.connectors.registry import Connector, connector

connector(Connector(name="youtube", label="YouTube", events=("video.published",), icon="youtube",
                    event_info={"video.published": ("Video published", "A new video goes live on the channel")}))
# Provider chips whose fires come from poll sources (triggers/poll_sources):
# a stored poll trigger with a non-http source publishes these connectors'
# events, scoped per trigger through the poll-name filter.
connector(Connector(name="google-sheets", label="Google Sheets", events=("row.new", "row.updated"), icon="table",
                    event_info={
                        "row.new": ("Row added", "A new row appears in the watched worksheet"),
                        "row.updated": ("Row changed", "An existing row in the watched worksheet is edited"),
                    }))
connector(Connector(name="google-drive", label="Google Drive",
                    events=("file.created", "file.updated", "file.deleted"),
                    icon="folder",
                    event_info={
                        "file.created": ("File created", "A new file appears in the watched Drive folder"),
                        "file.updated": ("File changed", "A file in the watched Drive folder is modified"),
                        "file.deleted": ("File deleted", "A file is removed from the watched Drive folder"),
                    }))
connector(Connector(name="google-calendar", label="Google Calendar",
                    events=("event.new",),
                    icon="calendar",
                    event_info={"event.new": ("Event created", "A new event is added to the watched calendar")}))
