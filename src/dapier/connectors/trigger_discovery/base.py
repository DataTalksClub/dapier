"""Trigger-discovery plumbing: the registry, its entry type and constants.

Import = registration: connector modules register :class:`TriggerDiscovery`
entries here, the catalog reads them back, and the other modules in this
package (samples, options, dispatch) share these primitives.
"""
from dataclasses import dataclass
from datetime import datetime, timezone

KINDS = ("sample", "options")
DEFAULT_LIMIT = 5
MAX_LIMIT = 25

# The sample envelope a discovery result MUST produce; ``dryrun.normalize_
# sample_event`` fills the engine fields (schema_version, correlation_id)
# around exactly these keys.
ENVELOPE_KEYS = ("connector", "event", "data", "id", "source", "occurred_at")


class DiscoveryNotFound(Exception):
    """Nothing discoverable: unknown connector/resource, or no data at all."""


class DiscoveryUpstream(Exception):
    """The live fetch failed, or its connection/credential prerequisites."""


@dataclass(frozen=True)
class TriggerDiscovery:
    """One discoverable capability of a trigger connector.

    ``kind`` is ``sample`` (a trigger event envelope; ``resource`` empty) or
    ``options`` (a field's ``[{value, label}]`` list, named by ``resource``
    — e.g. ``slack.channels``). ``fetch`` has the uniform signature
    ``(event=None, connection_id=None, limit=DEFAULT_LIMIT)`` and returns
    ``{"sample", "source", "connection_id"}`` for samples or
    ``{"options", "connection_id"}`` for options.
    """

    connector: str
    label: str
    kind: str
    resource: str
    fetch: object


TRIGGER_DISCOVERIES: dict = {}


def register_trigger_discovery(entry):
    """Register one trigger discovery; import = registration, like actions."""
    if entry.kind not in KINDS:
        raise ValueError(f"discovery kind must be one of: {', '.join(KINDS)}")
    TRIGGER_DISCOVERIES[(entry.connector, entry.kind, entry.resource)] = entry
    return entry


def trigger_discovery_catalog():
    """The ``discovery`` fragment of ``GET /api/catalog``."""
    samples = sorted({
        entry.connector for entry in TRIGGER_DISCOVERIES.values() if entry.kind == "sample"
    })
    options = {}
    for entry in TRIGGER_DISCOVERIES.values():
        if entry.kind == "options":
            options.setdefault(entry.connector, set()).add(entry.resource)
    return {
        "sample": samples,
        "options": {connector: sorted(resources) for connector, resources in sorted(options.items())},
    }


def _now_iso():
    return datetime.now(timezone.utc).isoformat()
