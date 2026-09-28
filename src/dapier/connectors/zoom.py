"""Zoom connector: meetings, past meetings, webinars and recordings
discovery, plus the health check.

Zoom connections come in two flavors: OAuth grants (refreshable tokens,
verified against ``users/me``) and webhook-only setups that store just a
signing secret. Both runners delegate to the shared provider layer; a
webhook-only connection reports a failed check with the refresh error
instead of pretending to be healthy.

The OAuth flavor also drives the ``zoom.recordings`` poll source: cloud
recordings fire ``recording.completed`` on a schedule with no Zoom app to
configure, the same event the webhook-only setups receive.
"""
import urllib.parse
from datetime import datetime, timedelta, timezone

from ..connections import discovery as provider
from ..engine.actions.zoom import (
    run_zoom_add_registrant,
    run_zoom_add_webinar_registrant,
    run_zoom_create_meeting,
    run_zoom_create_webinar,
    run_zoom_delete_meeting,
    run_zoom_delete_recording,
    run_zoom_delete_webinar,
    run_zoom_find_meeting,
    run_zoom_find_recording,
    run_zoom_find_webinar,
    run_zoom_list_past_participants,
    run_zoom_list_past_webinar_participants,
    run_zoom_update_meeting,
    run_zoom_update_webinar,
)
from ..triggers.poll_sources import PollSource, register_source
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
    description=("Find a Zoom meeting by id, or by topic among upcoming "
                 "meetings; with create_if_missing a missed topic find "
                 "creates the meeting instead and reports created: true "
                 "(Zapier's find-or-create)"),
    icon="zoom",
    run=lambda action, event, workflow_id, steps=None: run_zoom_find_meeting(
        action, event, steps=steps),
    required=frozenset({"connection_id"}),
    optional=frozenset({"meeting_id", "topic", "match", "scope",
                        "create_if_missing",
                        "start_time", "duration", "timezone", "agenda", "settings"}),
    fields=(
        {"key": "connection_id", "label": "Connection ID", "placeholder": "zoom",
         "required": True},
        {"key": "meeting_id", "label": "Meeting ID",
         "discover": {"resource": "zoom.meetings"}},
        {"key": "topic", "label": "Topic",
         "help": ("Used when no meeting id is given; searched across the "
                  "scope window — with Create if missing it names the created "
                  "meeting too")},
        {"key": "match", "label": "Topic match", "type": "select",
         "options": ["contains", "exact"], "default": "contains"},
        {"key": "scope", "label": "Scope", "type": "select",
         "options": ["upcoming", "past"], "default": "upcoming",
         "help": ("Where the topic search looks: upcoming (scheduled) "
                  "meetings by default, past ones already held with 'past'; "
                  "Create if missing needs the upcoming scope")},
        {"key": "create_if_missing", "label": "Create if missing", "type": "boolean",
         "help": ("When the topic find misses, create the meeting from Topic "
                  "and the fields below (needs the meeting:write scope; the "
                  "meeting-id path never creates)")},
        {"key": "start_time", "label": "Start time",
         "help": "Create-if-missing only: ISO 8601, e.g. 2026-10-01T09:00:00Z; "
                 "without it the created meeting is instant"},
        {"key": "duration", "label": "Duration (minutes)", "type": "number",
         "help": "Create-if-missing only; minutes"},
        {"key": "timezone", "label": "Time zone",
         "help": "Create-if-missing only; optional tz database name, e.g. Europe/Berlin"},
        {"key": "agenda", "label": "Agenda", "help": "Create-if-missing only"},
        {"key": "settings", "label": "Settings (JSON)",
         "help": "Create-if-missing only: Zoom meeting settings object; {tokens} expand inside and literal braces double, e.g. {{\"join_before_host\": true}}"},
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

register(Action(
    type="zoom_delete_recording",
    label="Zoom: delete recording",
    description=("Delete one meeting's cloud recording (DELETE "
                 "/meetings/{id}/recordings; Delete Recording). Action picks "
                 "how: trash (the default) is recoverable from Zoom's trash, "
                 "permanent destroys the recording and its files. Output: "
                 "{deleted: true, meeting_id, action}. Chain "
                 "zoom_find_recording's meeting_id."),
    icon="zoom",
    run=lambda action, event, workflow_id, steps=None: run_zoom_delete_recording(
        action, event, steps=steps),
    required=frozenset({"connection_id", "meeting_id"}),
    optional=frozenset({"action"}),
    fields=(
        {"key": "connection_id", "label": "Connection ID", "placeholder": "zoom",
         "required": True},
        {"key": "meeting_id", "label": "Meeting ID", "required": True,
         "discover": {"resource": "zoom.recordings"},
         "help": "The recorded meeting's id — zoom_find_recording's output "
                 "or a recording.completed event's meeting_id"},
        {"key": "action", "label": "Action", "type": "select",
         "options": ["trash", "permanent"], "default": "trash",
         "help": "Trash keeps the recording recoverable in Zoom's trash; "
                 "permanent destroys it and its files outright"},
    ),
))

register(Action(
    type="zoom_create_meeting",
    label="Zoom: create meeting",
    description="Create a Zoom meeting — scheduled when a start time is given, instant otherwise (Create Meeting)",
    icon="zoom",
    run=lambda action, event, workflow_id, steps=None: run_zoom_create_meeting(
        action, event, steps=steps),
    required=frozenset({"connection_id", "topic"}),
    optional=frozenset({"start_time", "duration", "timezone", "agenda", "settings"}),
    fields=(
        {"key": "connection_id", "label": "Connection ID", "placeholder": "zoom",
         "required": True},
        {"key": "topic", "label": "Topic", "required": True},
        {"key": "start_time", "label": "Start time",
         "help": "ISO 8601, e.g. 2026-10-01T09:00:00Z; without it Zoom creates an instant meeting"},
        {"key": "duration", "label": "Duration (minutes)", "type": "number",
         "default": "60",
         "help": "Scheduled meetings only; minutes"},
        {"key": "timezone", "label": "Time zone",
         "help": "Optional tz database name, e.g. Europe/Berlin"},
        {"key": "agenda", "label": "Agenda"},
        {"key": "settings", "label": "Settings (JSON)",
         "help": "Zoom meeting settings object; {tokens} expand inside and literal braces double, e.g. {{\"join_before_host\": true}}"},
    ),
))

register(Action(
    type="zoom_update_meeting",
    label="Zoom: update meeting",
    description=("Update one Zoom meeting's schedule or metadata — only the "
                 "fields set are sent, the rest of the meeting stays "
                 "untouched (PATCH /meetings/{id}). Output: {updated: true, "
                 "meeting_id, updated_fields}."),
    icon="zoom",
    run=lambda action, event, workflow_id, steps=None: run_zoom_update_meeting(
        action, event, steps=steps),
    required=frozenset({"connection_id", "meeting_id"}),
    optional=frozenset({"topic", "start_time", "duration", "timezone",
                        "agenda", "settings"}),
    fields=(
        {"key": "connection_id", "label": "Connection ID", "placeholder": "zoom",
         "required": True},
        {"key": "meeting_id", "label": "Meeting ID", "required": True,
         "discover": {"resource": "zoom.meetings"}},
        {"key": "topic", "label": "Topic",
         "help": "The meeting's new title; left out, Zoom keeps the old one"},
        {"key": "start_time", "label": "Start time",
         "help": "ISO 8601, e.g. 2026-10-01T09:00:00Z — the reschedule field"},
        {"key": "duration", "label": "Duration (minutes)", "type": "number",
         "help": "Scheduled meetings only; minutes"},
        {"key": "timezone", "label": "Time zone",
         "help": "Optional tz database name, e.g. Europe/Berlin"},
        {"key": "agenda", "label": "Agenda"},
        {"key": "settings", "label": "Settings (JSON)",
         "help": "Zoom meeting settings object; {tokens} expand inside and literal braces double, e.g. {{\"join_before_host\": true}}"},
    ),
))

register(Action(
    type="zoom_delete_meeting",
    label="Zoom: delete meeting",
    description=("Delete one Zoom meeting (DELETE /meetings/{id}). A "
                 "recurring meeting's whole series goes away unless "
                 "occurrence_id scopes the delete to one occurrence. "
                 "Output: {deleted: true, meeting_id}."),
    icon="zoom",
    run=lambda action, event, workflow_id, steps=None: run_zoom_delete_meeting(
        action, event, steps=steps),
    required=frozenset({"connection_id", "meeting_id"}),
    optional=frozenset({"occurrence_id"}),
    fields=(
        {"key": "connection_id", "label": "Connection ID", "placeholder": "zoom",
         "required": True},
        {"key": "meeting_id", "label": "Meeting ID", "required": True,
         "discover": {"resource": "zoom.past_meetings"}},
        {"key": "occurrence_id", "label": "Occurrence ID",
         "help": "Recurring meetings only: deletes just this occurrence — "
                 "left out, the whole series is deleted"},
    ),
))

register(Action(
    type="zoom_add_registrant",
    label="Zoom: add registrant",
    description=("Register one person for a Zoom meeting that requires "
                 "registration (POST /meetings/{id}/registrants) and get "
                 "their personalized join link. Output: {registered: true, "
                 "meeting_id, registrant_id, join_url} — join_url is unique "
                 "per registrant, the thing an invite email templates."),
    icon="zoom",
    run=lambda action, event, workflow_id, steps=None: run_zoom_add_registrant(
        action, event, steps=steps),
    required=frozenset({"connection_id", "meeting_id", "email"}),
    optional=frozenset({"first_name", "last_name"}),
    fields=(
        {"key": "connection_id", "label": "Connection ID", "placeholder": "zoom",
         "required": True},
        {"key": "meeting_id", "label": "Meeting ID", "required": True,
         "discover": {"resource": "zoom.meetings"}},
        {"key": "email", "label": "Email", "type": "email", "required": True,
         "placeholder": "{trigger.email}"},
        {"key": "first_name", "label": "First name"},
        {"key": "last_name", "label": "Last name"},
    ),
))

register(Action(
    type="zoom_list_past_participants",
    label="Zoom: list past meeting participants",
    description=("List who attended one past Zoom meeting (GET "
                 "/past_meetings/{id}/participants) — name, email and "
                 "join/leave times per attendee, up to 300 across three "
                 "pages. Output: {participants, count, meeting_id}; pair "
                 "with the meeting.ended trigger."),
    icon="zoom",
    run=lambda action, event, workflow_id, steps=None: run_zoom_list_past_participants(
        action, event, steps=steps),
    required=frozenset({"connection_id", "meeting_id"}),
    optional=frozenset(),
    fields=(
        {"key": "connection_id", "label": "Connection ID", "placeholder": "zoom",
         "required": True},
        {"key": "meeting_id", "label": "Meeting ID", "required": True,
         "discover": {"resource": "zoom.past_meetings"},
         "help": "the meeting's UUID or id — pair with the meeting.ended trigger"},
    ),
))

register(Action(
    type="zoom_create_webinar",
    label="Zoom: create webinar",
    description=("Create a Zoom webinar on the connected account — scheduled "
                 "when a start time is given, recurring with no fixed time "
                 "otherwise (Create Webinar). Output: {created: true, "
                 "scheduled, webinar: {id, topic, join_url, start_url, "
                 "passcode, ...}}."),
    icon="zoom",
    run=lambda action, event, workflow_id, steps=None: run_zoom_create_webinar(
        action, event, steps=steps),
    required=frozenset({"connection_id", "topic"}),
    optional=frozenset({"start_time", "duration", "timezone", "agenda", "settings"}),
    fields=(
        {"key": "connection_id", "label": "Connection ID", "placeholder": "zoom",
         "required": True},
        {"key": "topic", "label": "Topic", "required": True},
        {"key": "start_time", "label": "Start time",
         "help": "ISO 8601, e.g. 2026-10-01T09:00:00Z; without it Zoom creates "
                 "a recurring webinar with no fixed time"},
        {"key": "duration", "label": "Duration (minutes)", "type": "number",
         "default": "60",
         "help": "Scheduled webinars only; minutes"},
        {"key": "timezone", "label": "Time zone",
         "help": "Optional tz database name, e.g. Europe/Berlin"},
        {"key": "agenda", "label": "Agenda"},
        {"key": "settings", "label": "Settings (JSON)",
         "help": "Zoom webinar settings object; {tokens} expand inside and literal braces double, e.g. {{\"approval_type\": 2}}"},
    ),
))

register(Action(
    type="zoom_update_webinar",
    label="Zoom: update webinar",
    description=("Update one Zoom webinar's schedule or metadata — only the "
                 "fields set are sent, the rest of the webinar stays "
                 "untouched (PATCH /webinars/{id}). Output: {updated: true, "
                 "webinar_id, updated_fields}."),
    icon="zoom",
    run=lambda action, event, workflow_id, steps=None: run_zoom_update_webinar(
        action, event, steps=steps),
    required=frozenset({"connection_id", "webinar_id"}),
    optional=frozenset({"topic", "start_time", "duration", "timezone",
                        "agenda", "settings"}),
    fields=(
        {"key": "connection_id", "label": "Connection ID", "placeholder": "zoom",
         "required": True},
        {"key": "webinar_id", "label": "Webinar ID", "required": True,
         "discover": {"resource": "zoom.webinars"}},
        {"key": "topic", "label": "Topic",
         "help": "The webinar's new title; left out, Zoom keeps the old one"},
        {"key": "start_time", "label": "Start time",
         "help": "ISO 8601, e.g. 2026-10-01T09:00:00Z — the reschedule field"},
        {"key": "duration", "label": "Duration (minutes)", "type": "number",
         "help": "Scheduled webinars only; minutes"},
        {"key": "timezone", "label": "Time zone",
         "help": "Optional tz database name, e.g. Europe/Berlin"},
        {"key": "agenda", "label": "Agenda"},
        {"key": "settings", "label": "Settings (JSON)",
         "help": "Zoom webinar settings object; {tokens} expand inside and literal braces double, e.g. {{\"approval_type\": 2}}"},
    ),
))

register(Action(
    type="zoom_find_webinar",
    label="Zoom: find webinar",
    description=("Find a Zoom webinar by id, or by topic across upcoming "
                 "webinars — scope: past searches the ones already held. "
                 "Output: {found, webinar: {id, topic, start_time, "
                 "join_url, duration}}; a miss is found: false, not an error."),
    icon="zoom",
    run=lambda action, event, workflow_id, steps=None: run_zoom_find_webinar(
        action, event, steps=steps),
    required=frozenset({"connection_id"}),
    optional=frozenset({"webinar_id", "topic", "match", "scope"}),
    fields=(
        {"key": "connection_id", "label": "Connection ID", "placeholder": "zoom",
         "required": True},
        {"key": "webinar_id", "label": "Webinar ID",
         "discover": {"resource": "zoom.webinars"}},
        {"key": "topic", "label": "Topic",
         "help": "Used when no webinar id is given; searched across the "
                 "scope window"},
        {"key": "match", "label": "Topic match", "type": "select",
         "options": ["contains", "exact"], "default": "contains"},
        {"key": "scope", "label": "Scope", "type": "select",
         "options": ["upcoming", "past"], "default": "upcoming",
         "help": "Where the topic search looks: upcoming webinars by "
                 "default, past ones already held with 'past'"},
    ),
))

register(Action(
    type="zoom_add_webinar_registrant",
    label="Zoom: add webinar registrant",
    description=("Register one person for a Zoom webinar (POST "
                 "/webinars/{id}/registrants) and get their personalized "
                 "join link. Output: {registered: true, webinar_id, "
                 "registrant_id, join_url} — join_url is unique per "
                 "registrant, the thing an invite email templates."),
    icon="zoom",
    run=lambda action, event, workflow_id, steps=None: run_zoom_add_webinar_registrant(
        action, event, steps=steps),
    required=frozenset({"connection_id", "webinar_id", "email"}),
    optional=frozenset({"first_name", "last_name"}),
    fields=(
        {"key": "connection_id", "label": "Connection ID", "placeholder": "zoom",
         "required": True},
        {"key": "webinar_id", "label": "Webinar ID", "required": True,
         "discover": {"resource": "zoom.webinars"}},
        {"key": "email", "label": "Email", "type": "email", "required": True,
         "placeholder": "{trigger.email}"},
        {"key": "first_name", "label": "First name"},
        {"key": "last_name", "label": "Last name"},
    ),
))

register(Action(
    type="zoom_delete_webinar",
    label="Zoom: delete webinar",
    description=("Delete one Zoom webinar (DELETE /webinars/{id}). A "
                 "recurring webinar's whole series goes away unless "
                 "occurrence_id scopes the delete to one occurrence. "
                 "Output: {deleted: true, webinar_id}."),
    icon="zoom",
    run=lambda action, event, workflow_id, steps=None: run_zoom_delete_webinar(
        action, event, steps=steps),
    required=frozenset({"connection_id", "webinar_id"}),
    optional=frozenset({"occurrence_id"}),
    fields=(
        {"key": "connection_id", "label": "Connection ID", "placeholder": "zoom",
         "required": True},
        {"key": "webinar_id", "label": "Webinar ID", "required": True,
         "discover": {"resource": "zoom.webinars"}},
        {"key": "occurrence_id", "label": "Occurrence ID",
         "help": "recurring webinars only: deletes just this occurrence — "
                 "left out, the whole series is deleted"},
    ),
))

register(Action(
    type="zoom_list_past_webinar_participants",
    label="Zoom: list past webinar participants",
    description=("List who attended one past Zoom webinar (GET "
                 "/past_webinars/{id}/participants) — name, email and "
                 "join/leave times per attendee, up to 300 across three "
                 "pages. Output: {participants, count, webinar_id}; pair "
                 "with the webinar.ended trigger."),
    icon="zoom",
    run=lambda action, event, workflow_id, steps=None: (
        run_zoom_list_past_webinar_participants(action, event, steps=steps)),
    required=frozenset({"connection_id", "webinar_id"}),
    optional=frozenset(),
    fields=(
        {"key": "connection_id", "label": "Connection ID", "placeholder": "zoom",
         "required": True},
        {"key": "webinar_id", "label": "Webinar ID", "required": True,
         "discover": {"resource": "zoom.webinars"},
         "help": "the webinar's UUID or id — pair with the webinar.ended trigger"},
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
    name="past_meetings",
    connector="zoom",
    label="Past meetings",
    description="Meetings already held on the account, newest first",
    run=_listed("past_meetings"),
))

register_discovery(Discovery(
    name="recordings",
    connector="zoom",
    label="Recordings",
    description="Cloud recordings from the last 30 days",
    run=_listed("recordings"),
))

register_discovery(Discovery(
    name="webinars",
    connector="zoom",
    label="Webinars",
    description="Upcoming webinars on the account",
    run=_listed("webinars"),
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


def _fetch_past_meeting_options(event=None, connection_id=None, limit=DEFAULT_LIMIT):
    """Past-meeting options via the registry listing (first connected Zoom
    connection when no id is named) — the participants and cleanup flows'
    picker."""
    return trigger_discovery.options_from_registry(
        "zoom.past_meetings", connection_id, limit, provider="zoom",
        option_of=lambda item: {"value": item.get("id"),
                                "label": item.get("name") or item.get("id")})


register_trigger_discovery(TriggerDiscovery(
    connector="zoom", label="Zoom", kind="options", resource="zoom.past_meetings",
    fetch=_fetch_past_meeting_options))


def _fetch_webinar_options(event=None, connection_id=None, limit=DEFAULT_LIMIT):
    """Webinar options via the registry listing (first connected Zoom
    connection when no id is named)."""
    return trigger_discovery.options_from_registry(
        "zoom.webinars", connection_id, limit, provider="zoom",
        option_of=lambda item: {"value": item.get("id"),
                                "label": item.get("name") or item.get("id")})


register_trigger_discovery(TriggerDiscovery(
    connector="zoom", label="Zoom", kind="options", resource="zoom.webinars",
    fetch=_fetch_webinar_options))


def _fetch_recording_options(event=None, connection_id=None, limit=DEFAULT_LIMIT):
    """Recording options via the registry listing (first connected Zoom
    connection when no id is named). The value is the meeting id the
    recording actions carry, labeled with the topic."""
    return trigger_discovery.options_from_registry(
        "zoom.recordings", connection_id, limit, provider="zoom",
        option_of=lambda item: {"value": item.get("id"),
                                "label": item.get("name") or item.get("id")})


register_trigger_discovery(TriggerDiscovery(
    connector="zoom", label="Zoom", kind="options", resource="zoom.recordings",
    fetch=_fetch_recording_options))


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


# --- poll source: "New Recording" on the poll-trigger schedule ---------------
#
# recording.completed fires from Zoom's webhooks (triggers.intake.zoom_
# webhooks) — but those need a Zoom app with the Event Subscription and the
# signed endpoint validated. The ``zoom.recordings`` source is the no-app-
# config path: a stored poll trigger lists the connected account's cloud
# recordings on the schedule machinery (triggers.poll_sources) and publishes
# the same ``zoom``/``recording.completed`` events, so a workflow matches the
# same chip either way. Like the webhook path, only recordings carrying a
# video file (MP4/M4V) fire, and the first fire seeds the cursor without
# emitting — enabling a trigger must not fire the last month of history.

ZOOM_POLL_PAGE_SIZE = 100
ZOOM_POLL_PAGES = 3  # ~300 recordings per fire, the discovery listing's order
ZOOM_POLL_LOOKBACK_DAYS = 30
# The video-file filter recording.completed publishes (zoom_webhooks).
ZOOM_POLL_FILE_TYPES = frozenset({"MP4", "M4V"})


def _zoom_poll_validate(body):
    """Save-time fetch spec: the ``connection_id`` of the Zoom connection to
    poll as (required — the fetch refreshes its OAuth token), the optional
    ``for_email`` mailbox (default ``me`` — the connection's own user),
    plus the fetch defaults every stored zoom poll carries."""
    from ..triggers.email_triggers import TriggerError

    body = body if isinstance(body, dict) else {}
    if not str(body.get("connection_id") or "").strip():
        raise TriggerError(
            "connection_id is required: pick the Zoom connection to poll as")
    return {
        "for_email": str(body.get("for_email") or "").strip() or "me",
        "cursor_mode": "next_cursor",
        "id_path": "id",
        "url": "",
    }


def _zoom_poll_item(meeting):
    """One recording flattened to the shape the ``recording.completed``
    webhook publishes (``zoom_webhooks.recording_data``): meeting identity,
    topic, start time, share URL, and the video files' metadata with their
    download URLs — so downstream templates read the same keys no matter
    which path fired. Non-video-only recordings return None, mirroring the
    webhook's "at least one MP4 or M4V" rule.

    ``id`` is the watermark key: start time and recording uuid composed, so
    ISO text comparison orders recordings chronologically while the uuid
    keeps same-minute recordings distinct (the s3 source composes
    ``last_modified|key`` for the same reason).
    """
    videos = [
        {
            "id": file.get("id"),
            "file_type": file.get("file_type"),
            "recording_type": file.get("recording_type"),
            "file_size": file.get("file_size"),
            "play_url": file.get("play_url"),
            "download_url": file.get("download_url"),
        }
        for file in (meeting.get("recording_files") or [])
        if isinstance(file, dict)
        and str(file.get("file_type", "")).upper() in ZOOM_POLL_FILE_TYPES
    ]
    if not videos:
        return None
    uuid = str(meeting.get("uuid") or "")
    start_time = str(meeting.get("start_time") or "")
    return {
        "id": f"{start_time}|{uuid}",
        "meeting_id": str(meeting.get("id")),
        "meeting_uuid": uuid,
        "topic": meeting.get("topic"),
        "host_id": meeting.get("host_id"),
        "start_time": start_time,
        "share_url": meeting.get("share_url"),
        "download_url": videos[0].get("download_url") or "",
        "video_files": videos,
    }


def _zoom_poll_recordings(token, for_email, *, transport=None):
    """The mailbox's cloud recordings from the last
    ZOOM_POLL_LOOKBACK_DAYS days, following continuation pages up to
    ZOOM_POLL_PAGES through the provider's shared request path."""
    today = datetime.now(timezone.utc).date()
    params = {
        "from": (today - timedelta(days=ZOOM_POLL_LOOKBACK_DAYS)).isoformat(),
        "to": today.isoformat(),
        "per_page": ZOOM_POLL_PAGE_SIZE,
    }
    meetings = []
    for _page in range(ZOOM_POLL_PAGES):
        url = (f"{provider.ZOOM_API_URL}/users/{urllib.parse.quote(for_email, safe='')}"
               "/recordings?" + urllib.parse.urlencode(params))
        data = provider._request("GET", url, token, None, transport=transport)
        page = [entry for entry in data.get("meetings") or []
                if isinstance(entry, dict) and entry.get("uuid")]
        meetings.extend(page)
        next_page = data.get("next_page_token")
        if not page or not next_page:
            return meetings
        params = {**params, "next_page_token": next_page}
    return meetings


def _zoom_poll_fetch(item, cursor=None, *, transport=None):
    """One poll page as ``(items, next_cursor)`` for ``triggers.poll_sources``.

    Lists the mailbox's cloud recordings through Zoom's
    ``/users/{for_email}/recordings`` with the bearer token from
    ``poll_triggers._bearer_token``, so the connection's OAuth token is
    refreshed exactly like the classic fetch. Items are the recordings the
    ``recording.completed`` webhook would have published (see
    ``_zoom_poll_item``), oldest first.

    The cursor is the composite ``start_time|uuid`` watermark, which text
    comparison orders chronologically. With no stored cursor — the first
    fire after enabling — the fetch only seeds the watermark at the newest
    recording already in the lookback window and emits nothing. With a
    cursor, only recordings strictly past it fire, and the parked cursor is
    the last fired recording (the incoming one when nothing qualifies;
    ``fire`` parks it only once the page drains). The 30-day window
    deliberately overlaps between fires: the cursor filters the re-listed
    history and the seen store dedupes what slips past it. Raises
    ``RuntimeError`` on a failed fetch, like every poll source.
    """
    from ..triggers import poll_triggers

    if not str(item.get("connection_id") or "").strip():
        raise RuntimeError("poll source 'zoom.recordings' needs connection_id: "
                           "the Zoom connection to poll as")
    for_email = str(item.get("for_email") or "").strip() or "me"
    token = poll_triggers._bearer_token(item["connection_id"])
    try:
        meetings = _zoom_poll_recordings(token, for_email, transport=transport)
    except provider.DiscoveryError as exc:
        raise RuntimeError(f"zoom poll failed: {exc}") from None
    recordings = sorted(
        (entry for entry in (_zoom_poll_item(meeting) for meeting in meetings)
         if entry is not None),
        key=lambda entry: entry["id"])
    if cursor is None:
        # First fire: seed the watermark at the newest recording already in
        # the window (None while the account has none) without emitting.
        return [], (recordings[-1]["id"] if recordings else None)
    watermark = str(cursor)
    fresh = [entry for entry in recordings if entry["id"] > watermark]
    return fresh, (fresh[-1]["id"] if fresh else watermark)


def _zoom_poll_view(item):
    """The provider params ``public_view`` shows beside ``source``."""
    return {"for_email": item.get("for_email")}


register_source(PollSource(
    name="zoom.recordings", connector="zoom", event="recording.completed",
    label="Zoom", validate=_zoom_poll_validate,
    fetch=_zoom_poll_fetch, view=_zoom_poll_view))


# --- trigger samples, poll-aware ---------------------------------------------------

def _stored_zoom_poll(name):
    """The stored poll trigger named by ``event`` when it watches Zoom
    recordings, or None. Poll ids cannot contain dots and event names do, so
    a per-event ask never matches a poll; a missing selector, unconfigured
    poll triggers, an unknown name and a non-zoom source (the generic poll
    connector owns those) fold together: the caller only distinguishes
    live-vs-fallback, so any storage hiccup folds too — sampling never
    raises for want of infrastructure (see docs/connector-coverage-audit.md)."""
    from ..triggers import poll_triggers

    name = str(name or "").strip().lower()
    if not name or "." in name:
        return None
    try:
        item = poll_triggers.get_item(name)
    except Exception:
        return None
    if not item or str(item.get("source") or "") != "zoom.recordings":
        return None
    return item


# Older than any composite recording id: the chip's live sample pulls
# against this watermark, so the newest recording in the window answers
# before the trigger is even past its first (seeding) fire.
_ZOOM_EPOCH_CURSOR = "0000-01-01T00:00:00|"


def _fetch_zoom_sample(event=None, connection_id=None, limit=DEFAULT_LIMIT):
    """The Zoom chip's sample pull. An ``event`` naming a stored
    zoom.recordings poll pulls live through the poll's own fetch against the
    epoch watermark — no stored cursor is read or advanced — and wraps the
    newest recording in the envelope a real fire would publish. Anything
    else — a per-event ask, no stored poll yet, or a live fetch that cannot
    run (no connection, an unreachable Zoom) — falls through to the
    per-event chain: the newest recorded zoom run carrying the asked event,
    else the documented webhook example. A sample pull shows the payload
    shape, it never raises."""
    from ..triggers import poll_triggers

    item = _stored_zoom_poll(event)
    if item is not None:
        try:
            recordings, _next_cursor = _zoom_poll_fetch(item, _ZOOM_EPOCH_CURSOR)
            envelope = (poll_triggers.event_for(item, recordings[-1])
                        if recordings else None)
        except Exception:
            envelope = None
        if envelope is not None:
            return {
                "sample": trigger_discovery.as_sample(envelope),
                "source": "live",
                "connection_id": item.get("connection_id") or None,
            }
    return _PER_EVENT_ZOOM_SAMPLE(event=event, connection_id=connection_id,
                                  limit=limit)


register_trigger_discovery(TriggerDiscovery(
    connector="zoom", label="Zoom", kind="sample", resource="",
    fetch=_fetch_zoom_sample))
