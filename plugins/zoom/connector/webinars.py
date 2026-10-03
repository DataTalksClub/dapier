"""Zoom action registrations: webinars and their registrants."""
from plugins.zoom.runners import (
    run_zoom_add_webinar_registrant,
    run_zoom_create_webinar,
    run_zoom_delete_webinar,
    run_zoom_find_webinar,
    run_zoom_list_past_webinar_participants,
    run_zoom_update_webinar,
)
from src.dapier.connectors.registry import Action, register

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


