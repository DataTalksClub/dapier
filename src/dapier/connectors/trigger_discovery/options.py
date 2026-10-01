"""Options listings: resolve a connection, run a registry listing, map items.

Options listings that need an argument (a bucket, a spreadsheet id, a search
query) take it through the request's ``event`` field — the slot samples
already use: :func:`listing_params` splits it into the underlying registry
listing's params in order (the whole value for one-param listings,
"/"-separated for multi-param ones), and a missing required value is 404,
never a fabricated listing.

These are the trigger-side sibling of the connection-scoped listings in
``registry.DISCOVERIES`` (what a *connection* can list): options-delegating
fetches reuse those runners so there is exactly one provider call per
listing.
"""
import os

from .base import DEFAULT_LIMIT, DiscoveryNotFound, DiscoveryUpstream


def _through_package(name):
    """Resolve ``name`` on the package at call time, so tests patching
    ``trigger_discovery.<name>`` are honored by every submodule's fetches."""
    import importlib

    return getattr(importlib.import_module(__package__), name)


def connection_by_id(connection_id):
    """The stored connection record for an explicit id, or 404."""
    import boto3

    from ...connections import records as connections

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
    from ...connections import records as connections

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

    # No connections store configured (bare test/edge deploys) means nothing
    # live to discover, like api.discovery._load — fall through, never fail.
    if not os.environ.get("CONNECTIONS_TABLE"):
        raise DiscoveryNotFound(
            f"no connected {provider} connection to discover from; connect one first")
    table = boto3.resource("dynamodb").Table(os.environ["CONNECTIONS_TABLE"])
    for connection in connections.list_connections(table):
        if (connection.get("provider") == provider
                and connection.get("status") == connections.STATUS_CONNECTED):
            return connection
    raise DiscoveryNotFound(
        f"no connected {provider} connection to discover from; connect one first")


def stored_secret(credential_id, label):
    """The stored secret dict behind a connection/credential, or 502."""
    from ...connections import credentials

    try:
        return credentials.get_credential(credential_id)
    except KeyError:
        raise DiscoveryUpstream(f"{label}: no stored credential '{credential_id}'") from None


def _credential_connection(provider, credential_fallback):
    """The stand-in record when a shared credential (e.g. the stored Slack
    bot token) answers a listing with no connection behind it."""
    stored_secret(credential_fallback, f"{provider or credential_fallback} options")
    from ...connections import records as connections

    return {
        "provider": provider,
        "credential_id": credential_fallback,
        "status": connections.STATUS_CONNECTED,
    }


def _options_connection(provider, connection_id, *, credential_fallback=None):
    """The connection record an options listing runs against.

    Explicit id first, then the first connected connection of the provider;
    ``credential_fallback`` names a shared credential (e.g. the stored Slack
    bot token) that stands in for a connection when none exists.
    """
    from ...connections import records as connections

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
        return _credential_connection(provider, credential_fallback)
    raise DiscoveryNotFound(
        f"no connected {provider} connection to discover from; connect one first")


def listing_params(event, keys):
    """The registry-listing params carried in the ``event`` slot.

    A TriggerDiscovery fetch has no params field of its own, so the values a
    listing requires ride in ``event`` — the same slot the CLI's ``--event``
    and the API body's ``event`` already fill (the s3.objects bucket was the
    first user of the convention). ``keys`` names the listing's params in
    order: with one key the whole ``event`` is the value (paths and queries
    keep their "/"); with several, ``event`` splits on "/" positionally and
    an omitted tail keeps the provider's default (google-sheets rows:
    ``<spreadsheet_id>/<worksheet>``). A missing first value raises
    :class:`DiscoveryNotFound` — a typo stays a 404, not an empty listing.
    """
    raw = str(event or "").strip()
    parts = ([raw] if len(keys) == 1
             else [part.strip() for part in raw.split("/", len(keys) - 1)])
    params = {}
    for index, key in enumerate(keys):
        value = parts[index] if index < len(parts) else ""
        if not value:
            if index == 0:
                raise DiscoveryNotFound(
                    f"{key} is required: pass it as 'event'"
                    + (f" ({'/'.join(keys)}, parts separated by '/')"
                       if len(keys) > 1 else ""))
            continue
        params[key] = value
    return params


def options_from_registry(resource, connection_id, limit, *, provider=None,
                          credential_fallback=None, option_of=None, params=None):
    """Run a connection-scoped registry listing and map its items to options.

    The registry entries (``registry.DISCOVERIES[resource]``) own the single
    provider call per listing; this maps their ``{"id", "name"}`` items to
    the ``{"value", "label"}`` options shape (an item with no name labels
    itself with its id). ``params`` carries the query parameters the listing
    requires (e.g. ``{"bucket": ...}`` for ``s3.objects``); an empty listing
    is a fine answer, not an error.
    """
    from .. import registry

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
    option_of = option_of or _id_name_option
    options = [option_of(item) for item in (items or []) if isinstance(item, dict)]
    return {"options": options[:limit], "connection_id": connection.get("connection_id")}


def _id_name_option(item):
    return {"value": item.get("id"), "label": item.get("name") or item.get("id")}


def id_name_options(resource, *, provider=None, credential_fallback=None, params=None):
    """An options fetch bound to ``resource`` with the default id/name mapping.

    The one-liner most ``kind="options"`` registrations need: the listing's
    items come back as ``{"value": id, "label": name-or-id}`` through
    :func:`options_from_registry`'s default mapping.
    """
    def fetch(event=None, connection_id=None, limit=DEFAULT_LIMIT):
        options_from_registry = _through_package("options_from_registry")
        return options_from_registry(resource, connection_id, limit, provider=provider,
                                     credential_fallback=credential_fallback, params=params)
    return fetch
