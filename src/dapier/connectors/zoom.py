"""Zoom connector: meetings and recordings discovery, plus the health check.

Zoom connections come in two flavors: OAuth grants (refreshable tokens,
verified against ``users/me``) and webhook-only setups that store just a
signing secret. Both runners delegate to the shared provider layer; a
webhook-only connection reports a failed check with the refresh error
instead of pretending to be healthy.
"""
from ..connections import discovery as provider
from ..engine.actions.zoom import run_zoom_find_meeting
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
    description="Find a Zoom meeting by id, or by topic among upcoming meetings",
    icon="zoom",
    run=lambda action, event, workflow_id, steps=None: run_zoom_find_meeting(
        action, event, steps=steps),
    required=frozenset({"connection_id"}),
    optional=frozenset({"meeting_id", "topic", "match"}),
    fields=(
        {"key": "connection_id", "label": "Connection ID", "placeholder": "zoom",
         "required": True},
        {"key": "meeting_id", "label": "Meeting ID",
         "discover": {"resource": "zoom.meetings"}},
        {"key": "topic", "label": "Topic",
         "help": "Used when no meeting id is given; searched across upcoming meetings"},
        {"key": "match", "label": "Topic match", "type": "select",
         "options": ["contains", "exact"], "default": "contains"},
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
    name="recordings",
    connector="zoom",
    label="Recordings",
    description="Cloud recordings from the last 30 days",
    run=_listed("recordings"),
))

register_connection_test(ConnectionTest(connector="zoom", run=_tested))


# --- trigger discovery: meeting options for the find action's id field ----------

from . import trigger_discovery  # noqa: E402
from .trigger_discovery import (  # noqa: E402
    DEFAULT_LIMIT,
    TriggerDiscovery,
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
