"""Trigger-connector discovery: pull sample data and field options from a source.

Zapier's "pull in sample data" moment, per trigger connector: the designer
and the CLI ask one endpoint for a realistic event a workflow would receive
(``kind="sample"``), or for a field's option list so the operator does not
type channel ids and bucket names by hand (``kind="options"``, e.g.
``resource="slack.channels"``).

Three sample sources, reported in the response's ``source``:

- ``live`` — fetched from the connected account right now (Telegram
  ``getUpdates``, a poll trigger's fetch path);
- ``history`` — the newest recorded run for the connector in run history;
- ``synthetic`` — a documented, realistic example for connectors whose
  traffic was never recorded (or never flows inbound, like schedule).

Adding a discovery is one :class:`TriggerDiscovery` entry registered by the
connector's own module (import = registration, like actions). ``fetch``
callables never raise raw exceptions upward: :class:`DiscoveryNotFound`
maps to 404 (unknown connector/resource, nothing discoverable) and
:class:`DiscoveryUpstream` to 502 (the live fetch or its prerequisites
failed). :func:`api_discover` is the single ``(status, payload)`` dispatch
the agent and admin routes share.
"""
from .base import (DEFAULT_LIMIT, ENVELOPE_KEYS, KINDS, MAX_LIMIT,
                   DiscoveryNotFound, DiscoveryUpstream, TRIGGER_DISCOVERIES,
                   TriggerDiscovery, register_trigger_discovery,
                   trigger_discovery_catalog)
from .dispatch import api_discover, discover
from .options import (connected_connection, connection_by_id,
                      id_name_options, listing_params, options_from_registry,
                      stored_secret)
from .samples import (as_sample, history_or_synthetic_fetch, history_sample,
                      per_event_sample_fetch, synthetic_sample)

__all__ = [
    "DEFAULT_LIMIT", "ENVELOPE_KEYS", "KINDS", "MAX_LIMIT",
    "DiscoveryNotFound", "DiscoveryUpstream", "TRIGGER_DISCOVERIES",
    "TriggerDiscovery", "register_trigger_discovery",
    "trigger_discovery_catalog", "api_discover", "discover",
    "connected_connection", "connection_by_id", "id_name_options",
    "listing_params", "options_from_registry", "stored_secret",
    "as_sample", "history_or_synthetic_fetch", "history_sample",
    "per_event_sample_fetch", "synthetic_sample",
]
