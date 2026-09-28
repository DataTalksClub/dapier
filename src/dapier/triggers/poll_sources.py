"""Pluggable fetch sources for poll triggers.

The classic poll trigger fetches a JSON list over HTTP
(``poll_triggers.fetch_page_response``). A stored item's ``source`` key
swaps the fetcher while keeping everything else — the EventBridge schedule,
the cursor, the seen-set dedupe, the per-item fan-out — unchanged: ``s3``
lists a bucket through boto3, ``google-sheets.rows`` reads worksheet rows
through the Sheets API, ``google-drive.files`` lists a folder through the
Drive API, ``zoom.recordings`` lists cloud recordings through the Zoom API —
no Zoom app required — and ``dropbox.files``/``youtube.videos`` give the
webhook-only chips a poll path (a folder listing, an uploads playlist). A
source's events carry the source's own connector and event names
(``s3``/``file.created``), so workflows pick the matching palette chip while
the trigger still scopes runs through its ``poll`` filter.

A source registers itself with :func:`register_source` from its connector
module (connectors/s3.py, connectors/sheets.py, connectors/drive.py,
connectors/zoom.py, connectors/dropbox.py, connectors/youtube.py);
:func:`resolve`/:func:`stored_source` import the
built-in provider modules on first use so a scheduled fire never depends on
the engine having loaded the connectors package first. Provider modules
import their triggers siblings lazily, so the import stays cycle-free.
"""

DEFAULT_SOURCE = "http"

SOURCES: dict = {}

_BUILTIN_MODULES = ("..connectors.s3", "..connectors.sheets", "..connectors.drive",
                    "..connectors.zoom", "..connectors.dropbox", "..connectors.youtube",
                    "..connectors.rss",
                    "..connectors.mailchimp", "..connectors.slack",
                    "..connectors.calendar", "..connectors.gmail")
_loaded = False


class PollSource:
    """One named fetch source behind a poll trigger's ``source`` key.

    ``validate(body)`` — called by ``poll_triggers.build_item`` — checks the
    save's body and returns the fetch-spec fields to merge into the stored
    item: the provider params (``bucket``, ``spreadsheet_id``, ...) plus any
    fetch/cursor defaults (``cursor_mode``, ``id_path``, ``cursor_path``).
    It raises ``TriggerError`` with a save-shaped message. ``fetch(item,
    cursor)`` returns ``(items, next_cursor)`` for one fire — raw items the
    cursor/id machinery treats exactly like an HTTP poll page — and raises
    ``RuntimeError`` on a failed fetch. The optional ``view(item)`` returns
    the provider params ``public_view`` should show beside ``source``.
    """

    def __init__(self, name, connector, event, label, validate, fetch, view=None):
        self.name = name
        self.connector = connector
        self.event = event
        self.label = label
        self.validate = validate
        self.fetch = fetch
        self.view = view


def register_source(source):
    SOURCES[source.name] = source
    return source


def _load_builtins():
    global _loaded
    if _loaded:
        return
    import importlib

    for module in _BUILTIN_MODULES:
        importlib.import_module(module, __package__)
    _loaded = True


def source_names():
    """Every registered non-http source name (built-ins loaded on demand)."""
    _load_builtins()
    return sorted(SOURCES)


def resolve(name):
    """The fetch source for ``name``: None for the classic HTTP fetch, the
    registered source for a known non-http name; ``ValueError`` names the
    valid choices for an unknown one (build_item re-raises it as a save
    error)."""
    key = str(name or "").strip().lower() or DEFAULT_SOURCE
    if key == DEFAULT_SOURCE:
        return None
    _load_builtins()
    if key not in SOURCES:
        raise ValueError(f"source must be one of: {', '.join(['http'] + source_names())}")
    return SOURCES[key]


def stored_source(item):
    """The registered source of a stored poll item, or None for http.

    A stored non-http source always resolves — it passed save-time
    validation — so a miss here means the deployment dropped its module:
    fail the fire with a message instead of mis-fetching it as HTTP.
    """
    key = str((item or {}).get("source") or "").strip().lower()
    if not key or key == DEFAULT_SOURCE:
        return None
    _load_builtins()
    source = SOURCES.get(key)
    if source is None:
        raise RuntimeError(
            f"poll '{(item or {}).get('poll_id')}': source '{key}' is not registered")
    return source
