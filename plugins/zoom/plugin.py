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
                    icon="video"))
