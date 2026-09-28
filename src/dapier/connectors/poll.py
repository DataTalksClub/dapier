"""Poll trigger connector: a live list item as the discovery sample.

With ``event`` naming a stored poll trigger, discovery runs that trigger's
own fetch path once — no cursor is read or advanced, nothing is emitted —
and wraps the first listed item in the same envelope a real fire would
publish (``source: "live"``). The fallback chain is the chips' standard
one: newest recorded poll run (``"history"``), then a documented example
(``"synthetic"``) — the designer preview must always render. Two asks stay
deliberately precise: a named trigger whose fetch fails is a 502 (the
operator asked for that fetch specifically), and a name that matches no
stored poll trigger is a 404 rather than a fabricated sample.
"""

from . import trigger_discovery
from .trigger_discovery import (
    DEFAULT_LIMIT,
    DiscoveryNotFound,
    DiscoveryUpstream,
    TriggerDiscovery,
    register_trigger_discovery,
    synthetic_sample,
)

# Mirrors poll_triggers.POLL_EVENT; inlined to keep this module
# import-light (the triggers package pulls in the persistence layer).
POLL_EVENT = "item.new"

# A realistic listed item, wrapped the way ``poll_triggers.event_for`` wraps
# a real one (the item's own fields plus ``poll`` and ``item_id``).
_SYNTHETIC_ITEM = {
    "id": "4137",
    "title": "Invoice #4137 - September",
    "url": "https://example.test/posts/invoice-4137",
}


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
        if raw is not None:
            try:
                envelope = poll_triggers.event_for(item, raw)
            except ValueError as exc:
                raise DiscoveryUpstream(str(exc)) from exc
            return {
                "sample": trigger_discovery.as_sample(envelope),
                "source": "live",
                "connection_id": item.get("connection_id") or None,
            }
        # The trigger works but listed nothing new: nothing live to draw —
        # fall through to history, then the documented example.
    found = trigger_discovery.history_sample("poll")
    if found is not None:
        return {"sample": found, "source": "history", "connection_id": connection_id}
    if name and item is None:
        raise DiscoveryNotFound(f"no stored poll trigger named '{name}'")
    source = name or "discover"
    return {
        "sample": synthetic_sample("poll", POLL_EVENT,
                                   {**_SYNTHETIC_ITEM, "poll": source, "item_id": _SYNTHETIC_ITEM["id"]}),
        "source": "synthetic",
        "connection_id": connection_id,
    }


register_trigger_discovery(TriggerDiscovery(
    connector="poll", label="Poll", kind="sample", resource="",
    fetch=_fetch_sample))


def _fetch_trigger_options(event=None, connection_id=None, limit=DEFAULT_LIMIT):
    """The stored poll triggers, as options for the sample's event field.

    The operator picks the trigger to pull a live sample from instead of
    typing its name. No connection is involved, and an empty or unconfigured
    store is a fine empty list, never an error.
    """
    from ..triggers import poll_triggers

    try:
        items = poll_triggers.load_items()
    except Exception:  # store unconfigured (bare test/edge deploys): nothing to pick
        return {"options": [], "connection_id": None}
    options = []
    for item in items:
        name = item.get("poll_id")
        if not name:
            continue
        detail = str(item.get("description") or item.get("url") or "").strip()
        options.append({"value": name,
                        "label": f"{name} — {detail}" if detail else name})
    return {"options": options[:limit], "connection_id": None}


register_trigger_discovery(TriggerDiscovery(
    connector="poll", label="Poll", kind="options", resource="poll.triggers",
    fetch=_fetch_trigger_options))
