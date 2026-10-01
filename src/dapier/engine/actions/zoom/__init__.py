"""zoom_find_meeting / zoom_find_recording: look up Zoom meetings and cloud
recordings through an OAuth connection. zoom_create_meeting: create one
(scheduled or instant) meeting on the connected account.
zoom_update_meeting / zoom_add_registrant: reschedule one and register one
attendee on it. zoom_delete_meeting: remove one — or one occurrence of a
recurring one. zoom_list_past_participants: list who attended one past
meeting. The webinar runners (zoom_create_webinar, zoom_update_webinar,
zoom_find_webinar, zoom_add_webinar_registrant, zoom_delete_webinar,
zoom_list_past_webinar_participants) mirror the meeting shapes
one level up on Zoom's REST API.

Submodules: core (shared plumbing, views, field helpers), meetings,
create, recordings, registrants, webinars. Every runner resolves its
connection through this package namespace at call time, so tests patching
``engine.actions.zoom._zoom_connection`` reach them all.
"""
from .core import (  # noqa: F401 - the seam the tests patch through
    API_URL,
    DELETE_RECORDING_ACTIONS,
    FIND_SCOPES,
    MATCH_MODES,
    PARTICIPANTS_CAP,
    PARTICIPANTS_MAX_PAGES,
    PARTICIPANTS_PAGE_SIZE,
    _get_json,
    _request_json,
    _zoom_connection,
)
from .create import run_zoom_create_meeting, run_zoom_create_webinar
from .meetings import (run_zoom_delete_meeting, run_zoom_find_meeting,
                       run_zoom_list_past_participants,
                       run_zoom_update_meeting)
from .recordings import run_zoom_delete_recording, run_zoom_find_recording
from .registrants import (run_zoom_add_registrant,
                          run_zoom_add_webinar_registrant)
from .webinars import (run_zoom_delete_webinar, run_zoom_find_webinar,
                       run_zoom_list_past_webinar_participants,
                       run_zoom_update_webinar)
