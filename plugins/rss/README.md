# rss plugin

Any RSS 2.0 or Atom feed as a poll trigger — no connection, no
credential, just the feed URL.

## Surface

- **Trigger chip** `rss` / `item.new` — fired by the `rss` poll source:
  a stored poll trigger with `source: "rss"` fetches the feed on the
  poll schedule and publishes one event per new entry
  (`{id, title, link, published, summary}`; the id is the guid, else
  the Atom id, else the link).
- **Sample pull** — the chip's sample is the newest entry a stored rss
  poll's feed lists right now, else the newest recorded rss run, else a
  documented example.
- **No actions** — RSS is read-only; pair the trigger with any action
  (email, slack, sheets, ...).

## Fetch details

Stdlib only: urllib fetch (10s timeout, `dapier-poll-rss/1.0` user
agent) and `xml.etree` parsing, namespace-tolerant, tag-stripped
summaries capped at 500 chars. The cursor is the newest entry's id —
the first fire seeds it without emitting (a feed's existing entries
are history, not news); a feed that rotates its boundary entry off is
deduped by the poll's seen-set.

## Tests

```sh
make plugin-test PLUGIN=rss
```
