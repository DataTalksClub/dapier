"""The one discovery dispatch behind the agent and admin routes.

:func:`discover` resolves the request against the registry and never raises
raw exceptions upward: bad request fields raise ``ValueError`` (400 at the
API layer), unknown connector/resource or nothing discoverable raises
:class:`DiscoveryNotFound` (404), and a failed live fetch raises
:class:`DiscoveryUpstream` (502). :func:`api_discover` is the
``(status, payload)`` wrapper both ``/api/agent`` and ``/api/admin`` share,
so the console and the CLI cannot drift.
"""
from .base import (DEFAULT_LIMIT, KINDS, MAX_LIMIT, DiscoveryNotFound,
                   DiscoveryUpstream, TRIGGER_DISCOVERIES,
                   trigger_discovery_catalog, _now_iso)


def _coerce_limit(limit):
    try:
        limit = int(limit)
    except (TypeError, ValueError):
        raise ValueError(f"limit must be a number between 1 and {MAX_LIMIT}") from None
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


def _options_response(entry, result, limit, connection_id):
    options = [option for option in (result.get("options") or [])
               if isinstance(option, dict) and option.get("value") is not None]
    return {
        "connector": entry.connector,
        "resource": entry.resource,
        "options": options[:limit],
        "connection_id": connection_id,
    }


def _sample_response(entry, result, connection_id):
    return {
        "connector": entry.connector,
        "event": (result.get("sample") or {}).get("event"),
        "sample": result["sample"],
        "source": result.get("source"),
        "connection_id": connection_id,
        "fetched_at": _now_iso(),
    }


def discover(connector, *, kind="sample", resource=None, event=None,
             connection_id=None, limit=DEFAULT_LIMIT, identity=None):
    """Resolve and dispatch one discovery request; returns the response dict.

    ``identity`` is reserved for callers threading the acting subject;
    auditing happens in the API layers, which know it.

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
        return _options_response(entry, result, limit, resolved)
    return _sample_response(entry, result, resolved)


def api_discover(body):
    """The ``(status, payload)`` behind ``POST /api/agent|admin/discover``.

    Both routes parse their own request body and audit with their own
    subject; this is the one domain dispatch they share.
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
