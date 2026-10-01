"""Zoom discovery: connection-scoped listings, health check, trigger options."""
from ...connections import discovery as provider
from ..registry import (
    ConnectionTest,
    Discovery,
    register_connection_test,
    register_discovery,
)
from ..trigger_discovery import (
    TriggerDiscovery,
    id_name_options,
    register_trigger_discovery,
)

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




# --- trigger discovery: field options for the find actions' id fields -----------
#
# Every listing maps to {value: id, label: name} through the shared
# id_name_options helper; the first connected Zoom connection answers when
# the request names none.

register_trigger_discovery(TriggerDiscovery(
    connector="zoom", label="Zoom", kind="options", resource="zoom.meetings",
    fetch=id_name_options("zoom.meetings", provider="zoom")))

# The participants and cleanup flows' picker: past meetings, newest first.
register_trigger_discovery(TriggerDiscovery(
    connector="zoom", label="Zoom", kind="options", resource="zoom.past_meetings",
    fetch=id_name_options("zoom.past_meetings", provider="zoom")))

register_trigger_discovery(TriggerDiscovery(
    connector="zoom", label="Zoom", kind="options", resource="zoom.webinars",
    fetch=id_name_options("zoom.webinars", provider="zoom")))

# The value is the meeting id the recording actions carry, labeled with
# the topic.
register_trigger_discovery(TriggerDiscovery(
    connector="zoom", label="Zoom", kind="options", resource="zoom.recordings",
    fetch=id_name_options("zoom.recordings", provider="zoom")))
