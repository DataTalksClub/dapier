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

This is the trigger-side sibling of the connection-scoped listings in
:data:`~.registry.DISCOVERIES` (what a *connection* can list): options
delegating fetches reuse those runners so there is exactly one provider
call per listing, while samples own the event-envelope shape.

Adding a discovery is one :class:`TriggerDiscovery` entry registered by the
connector's own module (import = registration, like actions). ``fetch``
callables never raise raw exceptions upward: :class:`DiscoveryNotFound`
maps to 404 (unknown connector/resource, nothing discoverable) and
:class:`DiscoveryUpstream` to 502 (the live fetch or its prerequisites
failed). :func:`api_discover` is the single ``(status, payload)`` dispatch
the agent and admin routes share.
"""

import os
import uuid
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


# --- envelope helpers -----------------------------------------------------------


def _now_iso():
    return datetime.now(timezone.utc).isoformat()


def as_sample(envelope):
    """Project any dapier event envelope to the six discovery-sample keys."""
    data = envelope.get("data")
    sample = {
        "connector": str(envelope.get("connector") or ""),
        "event": str(envelope.get("event") or ""),
        "data": data if isinstance(data, dict) else {},
        "id": envelope.get("id"),
        "source": envelope.get("source"),
        "occurred_at": envelope.get("occurred_at"),
    }
    if not sample["id"]:
        sample["id"] = f"discover-{uuid.uuid4().hex[:12]}"
    if not sample["occurred_at"]:
        sample["occurred_at"] = _now_iso()
    return sample


def synthetic_sample(connector, event, data):
    """A documented realistic sample for connectors with no recorded traffic."""
    return {
        "connector": connector,
        "event": str(event or ""),
        "data": data,
        "id": f"discover-{uuid.uuid4().hex[:12]}",
        "source": connector,
        "occurred_at": _now_iso(),
    }


def history_sample(connector):
    """The newest recorded run for ``connector``, as a sample — or None.

    Run history stores one run per workflow handling of a trigger event; the
    same envelope rebuild the replay button uses (``runs.replay_event``)
    turns the newest run's recorded input back into a sample event.
    """
    from ..api import runs

    summaries = [
        summary for summary in runs.recent(limit=50)
        if summary.get("connector") == connector
    ]
    for summary in summaries:  # recent() is newest-first
        status, payload = runs.api_get(summary["run_id"])
        if status != 200:
            continue
        envelope, error = runs.replay_event(summary["run_id"], payload.get("steps") or [])
        if error or envelope is None:
            continue
        return as_sample(envelope)
    return None


def history_or_synthetic_fetch(connector, default_event, synthetic_data):
    """A sample fetch with the common fallback chain: history, then example.

    ``synthetic_data`` is the documented realistic payload used when the
    connector has no recorded runs; ``event`` renames the envelope's event
    on either path.
    """
    def fetch(event=None, connection_id=None, limit=DEFAULT_LIMIT):
        found = history_sample(connector)
        if found is not None:
            if event:
                found["event"] = event
            return {"sample": found, "source": "history", "connection_id": connection_id}
        return {
            "sample": synthetic_sample(connector, event or default_event, dict(synthetic_data)),
            "source": "synthetic",
            "connection_id": connection_id,
        }
    return fetch


# --- connection and credential helpers ------------------------------------------


def connection_by_id(connection_id):
    """The stored connection record for an explicit id, or 404."""
    import boto3

    from ..connections import records as connections

    table = boto3.resource("dynamodb").Table(os.environ["CONNECTIONS_TABLE"])
    connection = connections.get_connection(table, str(connection_id or "").strip())
    if not connection:
        raise DiscoveryNotFound(f"no connection named '{connection_id}'")
    return connection


def connected_connection(provider, connection_id=None):
    """A connected connection for live discovery: explicit id first.

    With no id, the first connected connection of ``provider`` wins, so
    discovery works before an operator memorizes connection ids. No usable
    account at all is 404 (nothing to discover from); a named connection
    that is not connected is 502 (the fetch cannot proceed).
    """
    from ..connections import records as connections

    if connection_id:
        connection = connection_by_id(connection_id)
        if connection.get("provider") != provider:
            raise DiscoveryUpstream(
                f"connection '{connection_id}' is a {connection.get('provider')} "
                f"connection, not {provider}")
        if connection.get("status") != connections.STATUS_CONNECTED:
            raise DiscoveryUpstream(
                f"connection '{connection_id}' is not connected; connect it first")
        return connection
    import boto3

    table = boto3.resource("dynamodb").Table(os.environ["CONNECTIONS_TABLE"])
    for connection in connections.list_connections(table):
        if (connection.get("provider") == provider
                and connection.get("status") == connections.STATUS_CONNECTED):
            return connection
    raise DiscoveryNotFound(
        f"no connected {provider} connection to discover from; connect one first")


def stored_secret(credential_id, label):
    """The stored secret dict behind a connection/credential, or 502."""
    from ..connections import credentials

    try:
        return credentials.get_credential(credential_id)
    except KeyError:
        raise DiscoveryUpstream(f"{label}: no stored credential '{credential_id}'") from None


# --- options delegation -----------------------------------------------------------

def _options_connection(provider, connection_id, *, credential_fallback=None):
    """The connection record an options listing runs against.

    Explicit id first, then the first connected connection of the provider;
    ``credential_fallback`` names a shared credential (e.g. the stored Slack
    bot token) that stands in for a connection when none exists.
    """
    from ..connections import records as connections

    if connection_id:
        connection = connection_by_id(connection_id)
        if provider and connection.get("provider") != provider:
            raise DiscoveryUpstream(
                f"connection '{connection_id}' is a {connection.get('provider')} "
                f"connection, not {provider}")
        if connection.get("status") != connections.STATUS_CONNECTED:
            raise DiscoveryUpstream(
                f"connection '{connection_id}' is not connected; connect it first")
        return connection
    if provider:
        import boto3

        table = boto3.resource("dynamodb").Table(os.environ["CONNECTIONS_TABLE"])
        for connection in connections.list_connections(table):
            if (connection.get("provider") == provider
                    and connection.get("status") == connections.STATUS_CONNECTED):
                return connection
    if credential_fallback:
        stored_secret(credential_fallback, f"{provider or credential_fallback} options")
        return {
            "provider": provider,
            "credential_id": credential_fallback,
            "status": connections.STATUS_CONNECTED,
        }
    raise DiscoveryNotFound(
        f"no connected {provider} connection to discover from; connect one first")


def options_from_registry(resource, connection_id, limit, *, provider=None,
                          credential_fallback=None, option_of=None, params=None):
    """Run a connection-scoped registry listing and map its items to options.

    The registry entries (``registry.DISCOVERIES[resource]``) own the single
    provider call per listing; this maps their ``{"id", "name"}`` items to
    the ``{"value", "label"}`` options shape. ``params`` carries the query
    parameters the listing requires (e.g. ``{"bucket": ...}`` for
    ``s3.objects``); an empty listing is a fine answer, not an error.
    """
    from . import registry

    entry = registry.DISCOVERIES.get(resource)
    if entry is None:
        raise DiscoveryUpstream(f"the '{resource}' listing is not registered")
    connection = _options_connection(provider, connection_id,
                                     credential_fallback=credential_fallback)
    try:
        items = entry.run(connection, dict(params or {}))
    except Exception as exc:
        raise DiscoveryUpstream(f"{resource} listing failed: "
                                f"{str(exc) or type(exc).__name__}") from exc
    option_of = option_of or (lambda item: {"value": item.get("id"), "label": item.get("name")})
    options = [option_of(item) for item in (items or []) if isinstance(item, dict)]
    return {"options": options[:limit], "connection_id": connection.get("connection_id")}


# --- dispatch ----------------------------------------------------------------------


def _coerce_limit(limit):
    try:
        limit = int(limit)
    except (TypeError, ValueError):
        raise ValueError("limit must be a number between 1 and 25") from None
    if not 1 <= limit <= MAX_LIMIT:
        raise ValueError(f"limit must be a number between 1 and {MAX_LIMIT}")
    return limit


def _not_found(connector, kind, resource):
    catalog = trigger_discovery_catalog()
    if connector not in catalog["sample"] and connector not in catalog["options"]:
        known = ", ".join(sorted(set(catalog["sample"]) | set(catalog["options"])))
        return DiscoveryNotFound(
            f"unknown discovery connector '{connector}'" + (f"; discoverable: {known}" if known else ""))
    if kind == "options":
        resources = catalog["options"].get(connector)
        if not resources:
            return DiscoveryNotFound(f"connector '{connector}' has no options resources")
        return DiscoveryNotFound(
            f"unknown resource '{resource}' for '{connector}'; discoverable: {', '.join(resources)}")
    return DiscoveryNotFound(f"connector '{connector}' has no sample discovery")


def discover(connector, *, kind="sample", resource=None, event=None,
             connection_id=None, limit=DEFAULT_LIMIT, identity=None):
    """Resolve and dispatch one discovery request; returns the response dict.

    Never raises raw exceptions upward: bad request fields raise
    ``ValueError`` (400 at the API layer), unknown connector/resource or
    nothing discoverable raises :class:`DiscoveryNotFound` (404), and a
    failed live fetch raises :class:`DiscoveryUpstream` (502). ``identity``
    is reserved for callers threading the acting subject; auditing happens
    in the API layers, which know it.

    ``event`` is the envelope's event-name override, except on connectors
    where it selects a source: for ``poll`` it names the stored poll trigger,
    and for ``schedule`` the schedule whose id lands in the sample data.
    """
    connector = str(connector or "").strip()
    kind = str(kind or "sample").strip().lower()
    if not connector:
        raise ValueError("connector is required")
    if kind not in KINDS:
        raise ValueError(f"kind must be one of: {', '.join(KINDS)}")
    if kind == "options" and not str(resource or "").strip():
        raise ValueError("resource is required when kind is 'options' (e.g. 'slack.channels')")
    limit = _coerce_limit(limit)
    event = str(event or "").strip() or None
    connection_id = str(connection_id or "").strip() or None
    resource = str(resource or "").strip()

    entry = TRIGGER_DISCOVERIES.get((connector, kind, resource))
    if entry is None:
        raise _not_found(connector, kind, resource)
    try:
        result = entry.fetch(event=event, connection_id=connection_id, limit=limit)
    except (DiscoveryNotFound, DiscoveryUpstream):
        raise
    except Exception as exc:  # a fetch bug must not escape as a raw traceback
        raise DiscoveryUpstream(f"{connector} discovery failed: {type(exc).__name__}") from exc

    resolved = result.get("connection_id") or connection_id
    if kind == "options":
        options = [option for option in (result.get("options") or [])
                   if isinstance(option, dict) and option.get("value") is not None]
        return {
            "connector": entry.connector,
            "resource": entry.resource,
            "options": options[:limit],
            "connection_id": resolved,
        }
    return {
        "connector": entry.connector,
        "event": (result.get("sample") or {}).get("event"),
        "sample": result["sample"],
        "source": result.get("source"),
        "connection_id": resolved,
        "fetched_at": _now_iso(),
    }


def api_discover(body):
    """The ``(status, payload)`` behind ``POST /api/agent|admin/discover``.

    Both routes parse their own request body and audit with their own
    subject; this is the one domain dispatch they share, so the console and
    the CLI cannot drift.
    """
    try:
        if not isinstance(body, dict):
            raise ValueError("request body must be an object")
        return 200, discover(
            body.get("connector"),
            kind=body.get("kind") or "sample",
            resource=body.get("resource"),
            event=body.get("event"),
            connection_id=body.get("connection_id"),
            limit=body.get("limit", DEFAULT_LIMIT),
        )
    except ValueError as exc:
        return 400, {"error": str(exc)}
    except DiscoveryNotFound as exc:
        return 404, {"error": str(exc)}
    except DiscoveryUpstream as exc:
        return 502, {"error": str(exc)}
