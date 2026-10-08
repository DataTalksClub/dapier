"""Zoom connector entry point: meetings/webinars/recordings actions,
discovery, the health check, the per-event webhook samples, and the
``zoom.recordings`` poll source.

The registration lives in the :mod:`plugins.zoom.connector` package
(moved verbatim from ``src/dapier/connectors/zoom/``) and the runners in
:mod:`plugins.zoom.runners`; importing them registers everything, like
every connector module. This file is the loader's entry point plus the
trigger chip.
"""
from plugins.zoom import connector  # noqa: F401  (import = registration)
from src.dapier.connectors.registry import Connector, connector

connector(Connector(name="zoom", label="Zoom",
                    events=("recording.completed", "recording.transcript_completed",
                            "meeting.started", "meeting.ended",
                            "meeting.registration_created",
                            "webinar.started", "webinar.ended",
                            "webinar.registration_created"),
                    icon="video",
                    event_info={
                        "recording.completed": ("Recording ready", "A cloud recording finishes processing"),
                        "recording.transcript_completed": ("Transcript ready", "A cloud recording's transcript is ready"),
                        "meeting.started": ("Meeting started", "A meeting begins"),
                        "meeting.ended": ("Meeting ended", "A meeting ends"),
                        "meeting.registration_created": ("Meeting registration", "Someone registers for a meeting"),
                        "webinar.started": ("Webinar started", "A webinar begins"),
                        "webinar.ended": ("Webinar ended", "A webinar ends"),
                        "webinar.registration_created": ("Webinar registration", "Someone registers for a webinar"),
                    }))
