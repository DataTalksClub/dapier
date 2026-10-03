"""RSS connector: any RSS 2.0 or Atom feed as a poll trigger, plus the
chip's sample pull.

A stored poll trigger with ``source: "rss"`` fetches the feed with urllib
(10s timeout) and parses it with stdlib xml.etree — no feed-parser
dependency. Entries keep the feed's own order (newest first, the RSS
convention); each becomes an ``rss``/``item.new`` event carrying
``{id, title, link, published, summary}`` — the id is the guid, else the
Atom id, else the link; the summary is tag-stripped, whitespace-collapsed
and capped. The cursor is the newest entry's id: the first fire seeds it
without emitting (a feed's existing entries are history, not news), later
fires emit the entries listed ahead of the cursor and the seen-set dedupes
a feed that rotated its boundary entry off.
"""
import html
import re
import urllib.request
import xml.etree.ElementTree as ET

from src.dapier.connectors import trigger_discovery
from src.dapier.connectors.registry import Connector, connector
from src.dapier.connectors.trigger_discovery import (
    DEFAULT_LIMIT,
    TriggerDiscovery,
    register_trigger_discovery,
)
from src.dapier.triggers.poll_sources import PollSource, register_source

connector(Connector(name="rss", label="RSS", events=("item.new",), icon="rss"))

RSS_TIMEOUT = 10
RSS_SUMMARY_CHARS = 500
RSS_USER_AGENT = "dapier-poll-rss/1.0"


def _rss_poll_validate(body):
    """Save-time fetch spec: ``url`` (the feed), with the fetch defaults a
    stored rss poll carries (next_cursor mode; items identify at ``id``)."""
    from src.dapier.triggers import poll_triggers

    body = body if isinstance(body, dict) else {}
    url = str(body.get("url") or "").strip()
    if not re.fullmatch(poll_triggers.URL_PATTERN, url):
        raise poll_triggers.TriggerError(
            "url is required: the http(s) URL of the RSS or Atom feed to watch")
    return {
        "url": url,
        "cursor_mode": "next_cursor",
        "id_path": "id",
    }


def _fetch_feed(url, *, timeout=RSS_TIMEOUT):
    request = urllib.request.Request(url, headers={"user-agent": RSS_USER_AGENT})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        if response.status >= 300:
            raise RuntimeError(f"rss fetch returned HTTP {response.status}")
        return response.read()


def _local(tag):
    return tag.rsplit("}", 1)[-1]


def _text(element):
    return "".join(element.itertext()) if element is not None else ""


def _collapsed(text):
    return re.sub(r"\s+", " ", text).strip()


def _strip_html(text, cap=RSS_SUMMARY_CHARS):
    """Readable text out of an HTML-ish summary: tags drop first (so an
    escaped entity never becomes a live tag), then entities unescape."""
    clean = _collapsed(html.unescape(re.sub(r"<[^>]+>", " ", text)))
    return clean[:cap].strip()


def _link_href(links):
    for link in links:  # Atom: rel="alternate" is the entry's canonical URL
        if link.get("rel") == "alternate" and link.get("href"):
            return link.get("href").strip()
    for link in links:
        if link.get("href"):
            return link.get("href").strip()
    for link in links:  # RSS 2.0: the link is text
        if link.text and link.text.strip():
            return link.text.strip()
    return ""


def _entry_of(entry):
    children = {}
    links = []
    for child in entry:
        name = _local(child.tag)
        children.setdefault(name, child)
        if name == "link":
            links.append(child)
    entry_id = (_text(children.get("guid")) or _text(children.get("id"))
                or _link_href(links)).strip()
    if not entry_id:
        return None  # nothing to identify the entry; the cursor machinery needs one
    return {
        "id": entry_id,
        "title": _collapsed(_text(children.get("title"))),
        "link": _link_href(links),
        "published": _collapsed(
            _text(children.get("pubDate")) or _text(children.get("published"))
            or _text(children.get("updated")) or _text(children.get("date"))),
        "summary": _strip_html(
            _text(children.get("description")) or _text(children.get("summary"))
            or _text(children.get("content"))),
    }


def _parse_feed(raw):
    """The feed's entries in document order (newest first, the convention
    both RSS 2.0 and Atom write), matched by local name so namespace
    prefixes stay irrelevant."""
    try:
        root = ET.fromstring(raw)
    except ET.ParseError as exc:
        raise RuntimeError(f"rss feed did not parse: {exc}") from None
    entries = (_entry_of(entry) for entry in root.iter()
               if _local(entry.tag) in ("item", "entry"))
    return [entry for entry in entries if entry is not None]


def _rss_poll_fetch(item, cursor=None):
    """One poll page as ``(items, next_cursor)`` for ``triggers.poll_sources``.

    The first fire (no stored cursor) seeds the cursor at the feed's newest
    id and emits nothing. Later fires emit the entries listed ahead of the
    cursor — stop scanning at the boundary entry — and the next cursor is
    the newest listed id (the incoming one when nothing qualifies; ``fire``
    parks it only after the page drains). A feed that rotated the boundary
    off re-lists everything, which the poll's seen-set dedupes. Raises
    ``RuntimeError`` on a failed fetch, like every poll source.
    """
    url = str(item.get("url") or "").strip()
    if not url:
        raise RuntimeError("poll source 'rss' needs a stored url")
    try:
        entries = _parse_feed(_fetch_feed(url))
    except Exception as exc:
        raise RuntimeError(f"rss fetch failed for {url}: "
                           f"{str(exc) or type(exc).__name__}") from exc
    newest = entries[0]["id"] if entries else None
    if cursor is None:
        return [], newest
    boundary = str(cursor)
    fresh = []
    for entry in entries:
        if entry["id"] == boundary:
            break
        fresh.append(entry)
    return fresh, (fresh[0]["id"] if fresh else boundary)


def _rss_poll_view(item):
    """The provider params ``public_view`` shows beside ``source``."""
    return {"url": item.get("url")}


register_source(PollSource(
    name="rss", connector="rss", event="item.new", label="RSS",
    validate=_rss_poll_validate, fetch=_rss_poll_fetch, view=_rss_poll_view))


def _stored_rss_poll(name):
    """The stored poll trigger named by ``event`` when it watches RSS, or
    None. A missing selector, unconfigured poll triggers, an unknown name
    and a non-rss source (the generic poll connector owns those) fold
    together: the caller only distinguishes live-vs-fallback, so any
    storage hiccup folds too — sampling never raises for want of
    infrastructure (see docs/connector-coverage-audit.md)."""
    from src.dapier.triggers import poll_triggers

    if not name:
        return None
    try:
        item = poll_triggers.get_item(name)
    except Exception:
        return None
    if not item or str(item.get("source") or "") != "rss":
        return None
    return item


_RSS_SYNTHETIC_ITEM = {
    "id": "https://example.test/blog/posts/invoice-pipelines",
    "title": "Invoice pipelines: from inbox to ledger",
    "link": "https://example.test/blog/posts/invoice-pipelines",
    "published": "Mon, 28 Sep 2026 09:14:00 GMT",
    "summary": "Every invoice email becomes a parsed, archived and "
               "follow-up-ready record — without anyone touching a "
               "spreadsheet.",
}


def _fetch_rss_sample(event=None, connection_id=None, limit=DEFAULT_LIMIT):
    """The RSS chip's sample pull: the newest entry a stored rss poll's feed
    lists right now (``source: "live"``), else the newest recorded rss run
    (``"history"``), else a documented example (``"synthetic"``).

    ``event`` names the stored poll trigger; only ``source: "rss"`` polls
    qualify. The live pull runs the poll's own fetch once against the
    "everything" cursor ``""`` — every entry lists ahead of it, so the
    feed's newest entry comes back; no stored cursor is read or advanced
    (the s3 sample's ``""``, rss-shaped). A live fetch that cannot run —
    no stored poll, an unreachable URL, a feed that does not parse — falls
    through to the recorded/documented sample instead of failing: a sample
    pull shows the payload shape, it never raises.
    """
    from src.dapier.triggers import poll_triggers

    name = str(event or "").strip().lower()
    item = _stored_rss_poll(name)
    if item is not None:
        try:
            entries, _next_cursor = _rss_poll_fetch(item, "")
            envelope = (poll_triggers.event_for(item, entries[0])
                        if entries else None)
        except Exception:
            envelope = None
        if envelope is not None:
            return {
                "sample": trigger_discovery.as_sample(envelope),
                "source": "live",
                "connection_id": item.get("connection_id") or None,
            }
    found = trigger_discovery.history_sample("rss")
    if found is not None:
        return {"sample": found, "source": "history", "connection_id": connection_id}
    return {
        "sample": trigger_discovery.synthetic_sample(
            "rss", "item.new", dict(_RSS_SYNTHETIC_ITEM)),
        "source": "synthetic",
        "connection_id": connection_id,
    }


register_trigger_discovery(TriggerDiscovery(
    connector="rss", label="RSS", kind="sample", resource="",
    fetch=_fetch_rss_sample))
