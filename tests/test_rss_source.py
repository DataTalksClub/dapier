"""The rss poll source: save validation, RSS 2.0 and Atom parsing (namespace
tolerant, tag-stripped summaries capped at 500), the seeded first fire and
newest-id cursor, the seen-set dedupe of a feed that re-lists its boundary,
the end-to-end fire, and the chip's sample pull (live → history → synthetic).

Zapier's "New feed item" over stdlib: a stored poll trigger with
``source: "rss"`` fetches the feed with urllib and parses it with
xml.etree; tests stub ``rss._fetch_feed``, the one seam the network hides
behind.
"""
from unittest.mock import patch

import pytest

from src.dapier.connectors import registry, rss as rss_connector
from src.dapier.connectors import trigger_discovery
from src.dapier.triggers import poll_sources, poll_triggers
from src.dapier.triggers.email_triggers import TriggerError

ACTIONS = [{"type": "email_send", "to": "reader@example.test"}]
FEED_URL = "https://example.test/feed.xml"


RSS_FEED = """<rss xmlns:dc="http://purl.org/dc/elements/1.1/" version="2.0">
  <channel>
    <title>Blog</title>
    <item>
      <guid isPermaLink="false">post-2</guid>
      <title>Second post</title>
      <link>https://example.test/posts/2</link>
      <pubDate>Mon, 28 Sep 2026 09:14:00 GMT</pubDate>
      <description><![CDATA[<p>  Hello   <b>rich</b> world </p><p>tail</p>]]></description>
    </item>
    <item>
      <guid>post-1</guid>
      <title>First post</title>
      <link>https://example.test/posts/1</link>
      <dc:date>Sun, 21 Sep 2026 08:00:00 GMT</dc:date>
      <description>Plain text</description>
    </item>
  </channel>
</rss>"""


ATOM_FEED = """<feed xmlns="http://www.w3.org/2005/Atom">
  <title>Feed</title>
  <entry>
    <id>tag:example.test,2026:post-9</id>
    <title>Atom entry</title>
    <link rel="alternate" href="https://example.test/posts/9"/>
    <updated>2026-09-28T10:00:00Z</updated>
    <summary>&lt;b&gt;Rich&lt;/b&gt; text  with entities</summary>
  </entry>
  <entry>
    <id>tag:example.test,2026:post-8</id>
    <title>Summaryless</title>
    <link href="https://example.test/posts/8"/>
    <published>2026-09-21T10:00:00Z</published>
    <content type="html">&lt;p&gt;Content body&lt;/p&gt;</content>
  </entry>
</feed>"""


def rss_body(**overrides):
    body = {
        "name": "feed-watch",
        "expression": "rate(1 hour)",
        "source": "rss",
        "url": FEED_URL,
        "actions": ACTIONS,
    }
    body.update(overrides)
    return body


def stored(body=None):
    """The stored item build_item would persist for ``body``."""
    return poll_triggers.build_item(body or rss_body(), "op@example.test")


def stub_feed(monkeypatch, raw):
    monkeypatch.setattr(rss_connector, "_fetch_feed", lambda url, **_kw: raw)


class FakeCursorTable:
    """DynamoDB stand-in for poll cursors and seen-sets (cursor_id key)."""

    def __init__(self):
        self.items = {}

    def get_item(self, Key):
        item = self.items.get(Key["cursor_id"])
        return {"Item": dict(item)} if item else {}

    def put_item(self, Item):
        self.items[Item["cursor_id"]] = dict(Item)

    def delete_item(self, Key):
        self.items.pop(Key["cursor_id"], None)


def run_fire(item, raw, *, cursors=None):
    """One scheduled fire against a canned feed, with real cursor/seen
    machinery and the engine stubbed out."""
    fired_events = []
    cursors = cursors or FakeCursorTable()

    with patch.object(poll_triggers, "get_item", return_value=item), \
         patch.object(rss_connector, "_fetch_feed", return_value=raw), \
         patch("src.dapier.engine.execute",
               side_effect=lambda event, **_kwargs: fired_events.append(event)), \
         patch("src.dapier.engine.notify.notify_failure"):
        result = poll_triggers.fire(item["poll_id"], cursor_table_ref=cursors)
    return result, fired_events, cursors


# --- registration -----------------------------------------------------------------


def test_the_rss_chip_registers_a_poll_source_and_a_sample():
    source = poll_sources.SOURCES["rss"]

    assert (source.connector, source.event, source.label) == ("rss", "item.new", "RSS")
    assert "rss" in poll_sources.source_names()
    chip = registry.CONNECTORS["rss"]
    assert chip.events == ("item.new",)
    assert chip.label == "RSS"
    assert "rss" in trigger_discovery.trigger_discovery_catalog()["sample"]


# --- save validation --------------------------------------------------------------


def test_save_stores_the_fetch_spec():
    item = stored()

    assert item["source"] == "rss"
    assert item["url"] == FEED_URL
    assert item["cursor_mode"] == "next_cursor"
    assert item["id_path"] == "id"


def test_save_requires_the_feed_url():
    with pytest.raises(TriggerError, match="url is required"):
        stored(rss_body(url=""))

    with pytest.raises(TriggerError, match="url is required"):
        stored(rss_body(url="ftp://example.test/feed.xml"))


def test_public_view_shows_the_feed():
    view = poll_triggers.public_view(stored())

    assert view["source"] == "rss"
    assert view["url"] == FEED_URL


# --- parsing ----------------------------------------------------------------------


def test_rss_20_entries_parse_with_stripped_summaries():
    entries = rss_connector._parse_feed(RSS_FEED.encode())

    assert entries == [
        {"id": "post-2", "title": "Second post",
         "link": "https://example.test/posts/2",
         "published": "Mon, 28 Sep 2026 09:14:00 GMT",
         "summary": "Hello rich world tail"},
        {"id": "post-1", "title": "First post",
         "link": "https://example.test/posts/1",
         "published": "Sun, 21 Sep 2026 08:00:00 GMT",
         "summary": "Plain text"},
    ]


def test_atom_entries_parse_without_namespace_noise():
    entries = rss_connector._parse_feed(ATOM_FEED.encode())

    assert entries[0] == {
        "id": "tag:example.test,2026:post-9", "title": "Atom entry",
        "link": "https://example.test/posts/9",
        "published": "2026-09-28T10:00:00Z",
        "summary": "Rich text with entities",
    }
    assert entries[1]["id"] == "tag:example.test,2026:post-8"
    assert entries[1]["link"] == "https://example.test/posts/8"
    assert entries[1]["published"] == "2026-09-21T10:00:00Z"
    assert entries[1]["summary"] == "Content body"


def test_identity_falls_back_to_link_and_unidentifiable_entries_are_dropped():
    feed = """<rss version="2.0"><channel>
      <item><link>https://example.test/by-link</link><title>Linked</title></item>
      <item><title>No identity at all</title></item>
    </channel></rss>"""

    entries = rss_connector._parse_feed(feed.encode())

    assert [entry["id"] for entry in entries] == ["https://example.test/by-link"]


def test_summaries_cap_at_500_characters():
    feed = ('<rss version="2.0"><channel><item><guid>long</guid>'
            '<description>' + "word " * 300 + '</description></item>'
            '</channel></rss>')

    entries = rss_connector._parse_feed(feed.encode())

    assert 400 < len(entries[0]["summary"]) <= 500


def test_an_unparseable_feed_fails_the_fetch():
    with pytest.raises(RuntimeError, match="did not parse"):
        rss_connector._parse_feed(b"<rss><channel><item><description>unclosed")


# --- fetch: seed, then entries listed ahead of the cursor --------------------------


def test_first_fetch_seeds_at_the_newest_id_without_emitting(monkeypatch):
    stub_feed(monkeypatch, RSS_FEED.encode())

    items, next_cursor = rss_connector._rss_poll_fetch(stored(), None)

    assert items == []
    assert next_cursor == "post-2"


def test_next_fetch_returns_only_entries_listed_ahead_of_the_cursor(monkeypatch):
    stub_feed(monkeypatch, RSS_FEED.encode())

    items, next_cursor = rss_connector._rss_poll_fetch(stored(), "post-1")

    assert [item["id"] for item in items] == ["post-2"]
    assert next_cursor == "post-2"


def test_nothing_new_keeps_the_cursor(monkeypatch):
    stub_feed(monkeypatch, RSS_FEED.encode())

    items, next_cursor = rss_connector._rss_poll_fetch(stored(), "post-2")

    assert items == []
    assert next_cursor == "post-2"


def test_the_everything_cursor_lists_the_whole_feed(monkeypatch):
    stub_feed(monkeypatch, RSS_FEED.encode())

    items, next_cursor = rss_connector._rss_poll_fetch(stored(), "")

    assert [item["id"] for item in items] == ["post-2", "post-1"]
    assert next_cursor == "post-2"


def test_a_feed_missing_the_boundary_relists_everything(monkeypatch):
    stub_feed(monkeypatch, RSS_FEED.encode())

    items, _next = rss_connector._rss_poll_fetch(stored(), "post-gone")

    assert [item["id"] for item in items] == ["post-2", "post-1"]


def test_a_missing_stored_url_fails_the_fetch():
    with pytest.raises(RuntimeError, match="needs a stored url"):
        rss_connector._rss_poll_fetch({"poll_id": "x", "source": "rss"}, None)


def test_a_failed_fetch_raises_runtimeerror(monkeypatch):
    def broken(url, **_kw):
        raise OSError("timed out")

    monkeypatch.setattr(rss_connector, "_fetch_feed", broken)

    with pytest.raises(RuntimeError, match="rss fetch failed"):
        rss_connector._rss_poll_fetch(stored(), "post-2")


# --- end-to-end fire ---------------------------------------------------------------


def test_fire_seeds_then_emits_only_the_new_entry():
    item = stored()
    cursors = FakeCursorTable()

    result, fired, cursors = run_fire(item, RSS_FEED.encode(), cursors=cursors)

    assert result == {"poll": "feed-watch", "fired": 0}
    assert fired == []
    assert poll_triggers.get_cursor("feed-watch", table=cursors) == "post-2"

    newer_feed = RSS_FEED.replace("<item>", """<item>
      <guid>post-3</guid><title>Third post</title>
      <link>https://example.test/posts/3</link>
      <description>Fresh</description>
    </item><item>""", 1)
    result, fired, _ = run_fire(item, newer_feed.encode(), cursors=cursors)

    assert result == {"poll": "feed-watch", "fired": 1}
    event = fired[0]
    assert event["connector"] == "rss"
    assert event["event"] == "item.new"
    assert event["source"] == "feed-watch"
    assert event["data"]["item_id"] == "post-3"
    assert event["data"]["id"] == "post-3"
    assert event["data"]["link"] == "https://example.test/posts/3"
    assert poll_triggers.get_cursor("feed-watch", table=cursors) == "post-3"


def test_fire_dedupes_a_relisted_entry_and_drains_the_rest():
    # A one-item-per-fire budget parks the cursor below the leftover entry,
    # so the next fire re-lists it — and must not re-run the one already
    # published (the seen-set's job).
    item = stored(rss_body(max_items=1))
    cursors = FakeCursorTable()

    run_fire(item, RSS_FEED.encode(), cursors=cursors)  # seed at post-2
    two_new = RSS_FEED.replace(
        "<item>",
        """<item>
      <guid>post-4</guid><title>Fourth post</title>
      <link>https://example.test/posts/4</link>
    </item><item>
      <guid>post-3</guid><title>Third post</title>
      <link>https://example.test/posts/3</link>
    </item><item>""", 1)

    result, fired, _ = run_fire(item, two_new.encode(), cursors=cursors)

    assert result == {"poll": "feed-watch", "fired": 1}
    assert [event["data"]["id"] for event in fired] == ["post-4"]
    assert poll_triggers.get_cursor("feed-watch", table=cursors) == "post-2"

    result, fired, _ = run_fire(item, two_new.encode(), cursors=cursors)

    assert result == {"poll": "feed-watch", "fired": 1, "skipped_seen": 1}
    assert [event["data"]["id"] for event in fired] == ["post-3"]
    assert poll_triggers.get_cursor("feed-watch", table=cursors) == "post-4"


def test_the_fired_workflow_matches_the_chip():
    workflow = poll_triggers.workflow_for(stored())

    assert workflow["trigger"] == {
        "connector": "rss", "event": "item.new",
        "filters": {"poll": {"equals": "feed-watch"}}}


# --- the chip's sample pull --------------------------------------------------------


def pin_no_history(monkeypatch):
    monkeypatch.setattr(trigger_discovery, "history_sample",
                        lambda connector, event=None: None)


def discover(**body):
    status, payload = trigger_discovery.api_discover({"kind": "sample", **body})
    return status, payload


def test_sample_pulls_the_stored_polls_newest_entry_live(monkeypatch):
    pin_no_history(monkeypatch)
    stub_feed(monkeypatch, RSS_FEED.encode())
    item = stored()
    monkeypatch.setattr(poll_triggers, "get_item",
                        lambda name, table_ref=None: item)

    status, payload = discover(connector="rss", event="feed-watch")

    assert status == 200, payload
    assert payload["source"] == "live"
    assert payload["sample"]["connector"] == "rss"
    assert payload["sample"]["event"] == "item.new"
    assert payload["sample"]["data"]["id"] == "post-2"  # the feed's newest entry
    assert payload["sample"]["data"]["poll"] == "feed-watch"


def test_sample_without_a_stored_poll_is_synthetic(monkeypatch):
    pin_no_history(monkeypatch)
    monkeypatch.setattr(poll_triggers, "get_item",
                        lambda name, table_ref=None: None)

    status, payload = discover(connector="rss", event="no-such-poll")

    assert status == 200, payload
    assert payload["source"] == "synthetic"
    assert payload["sample"]["event"] == "item.new"
    assert payload["sample"]["data"]["id"]
    assert payload["sample"]["data"]["link"].startswith("https://")
    assert payload["sample"]["data"]["summary"]


def test_sample_without_a_name_is_synthetic_too(monkeypatch):
    pin_no_history(monkeypatch)

    status, payload = discover(connector="rss")

    assert status == 200, payload
    assert payload["source"] == "synthetic"


def test_sample_ignores_a_poll_with_another_source(monkeypatch):
    pin_no_history(monkeypatch)
    item = stored(rss_body(source="http", id_path="id"))
    monkeypatch.setattr(poll_triggers, "get_item",
                        lambda name, table_ref=None: item)

    status, payload = discover(connector="rss", event="feed-watch")

    assert status == 200
    assert payload["source"] == "synthetic"


def test_sample_falls_back_when_the_live_fetch_fails(monkeypatch):
    pin_no_history(monkeypatch)
    item = stored()
    monkeypatch.setattr(poll_triggers, "get_item",
                        lambda name, table_ref=None: item)

    def broken(url, **_kw):
        raise OSError("timed out")

    monkeypatch.setattr(rss_connector, "_fetch_feed", broken)

    status, payload = discover(connector="rss", event="feed-watch")

    assert status == 200, payload  # a broken feed folds to the fallback
    assert payload["source"] == "synthetic"


if __name__ == "__main__":
    pytest.main([__file__])
