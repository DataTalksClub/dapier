"""Zoom action registrations: meetings, recordings and participants.

The runners live in :mod:`src.dapier.engine.actions.zoom`; this module only
declares the catalog entries (labels, fields, discovery wiring) the console
and the engine read.
"""
from ...engine.actions.zoom import (
    run_zoom_add_registrant,
    run_zoom_create_meeting,
    run_zoom_delete_meeting,
    run_zoom_delete_recording,
    run_zoom_find_meeting,
    run_zoom_find_recording,
    run_zoom_list_past_participants,
    run_zoom_update_meeting,
)
from ..registry import Action, register

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

