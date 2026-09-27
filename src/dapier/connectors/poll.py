"""Poll trigger connector: a live list item as the discovery sample.

With ``event`` naming a stored poll trigger, discovery runs that trigger's
own fetch path once — no cursor is read or advanced, nothing is emitted —
and wraps the first listed item in the same envelope a real fire would
publish (``source: "live"``). Otherwise the newest recorded poll run is the
sample (``"history"``); with neither there is nothing discoverable (404).
"""

from . import trigger_discovery
from .trigger_discovery import (
    DEFAULT_LIMIT,
    DiscoveryNotFound,
    DiscoveryUpstream,
    TriggerDiscovery,
    register_trigger_discovery,
)


def _stored_item(name):
    """The stored poll trigger named by ``event``, or None (selector absent,
    poll triggers unconfigured, or no trigger by that name — the caller
    distinguishes only live-vs-fallback, so all three fold together)."""
    from ..triggers import poll_triggers

    if not name:
        return None
    try:
        return poll_triggers.get_item(name)
    except poll_triggers.TriggerError:
        return None


def _fetch_sample(event=None, connection_id=None, limit=DEFAULT_LIMIT, transport=None):
    from ..triggers import poll_triggers

    name = str(event or "").strip().lower()
    item = _stored_item(name)
    if item is not None:
        try:
            items = poll_triggers.fetch_page(item, transport=transport)
        except Exception as exc:
            raise DiscoveryUpstream(
                f"poll '{name}' fetch failed: {str(exc) or type(exc).__name__}") from exc
        raw = next((entry for entry in items if entry is not None), None)
        if raw is None:
            raise DiscoveryNotFound(f"poll '{name}' listed no items")
        try:
            envelope = poll_triggers.event_for(item, raw)
        except ValueError as exc:
            raise DiscoveryUpstream(str(exc)) from exc
        return {
            "sample": trigger_discovery.as_sample(envelope),
            "source": "live",
            "connection_id": item.get("connection_id") or None,
        }
    found = trigger_discovery.history_sample("poll")
    if found is not None:
        return {"sample": found, "source": "history", "connection_id": connection_id}
    if name:
        raise DiscoveryNotFound(f"no stored poll trigger named '{name}'")
    raise DiscoveryNotFound(
        "no poll runs recorded yet; name a stored poll trigger with event to fetch one live")


register_trigger_discovery(TriggerDiscovery(
    connector="poll", label="Poll", kind="sample", resource="",
    fetch=_fetch_sample))
