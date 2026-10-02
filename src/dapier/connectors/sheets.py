"""Google Sheets connector: append rows through a Google connection, plus
the google-sheets discovery resources and the Google identity check.

The Discovery/ConnectionTest run callables registered here delegate to the
shared provider layer (``connections.discovery``) — the same code the
/api/*/connections/.../discover endpoints serve — so the catalog metadata
and the live listings can never drift apart.
"""
import hashlib
import json

from ..connections import discovery as provider
from ..engine.actions.sheets import (
    run_sheets_add_worksheet,
    run_sheets_append_row,
    run_sheets_clear_values,
    run_sheets_create_column,
    run_sheets_create_spreadsheet,
    run_sheets_delete_row,
    run_sheets_find_row,
    run_sheets_lookup_row,
    run_sheets_update_row,
)
from ..triggers.poll_sources import PollSource, register_source
from .registry import (
    Action,
    ConnectionTest,
    Discovery,
    register,
    register_connection_test,
    register_discovery,
)

register(Action(
    type="sheets_append_row",
    label="Google Sheets",
    icon="sheets",
    description="Append a row to a worksheet (Create Spreadsheet Row)",
    run=lambda action, event, workflow_id, steps=None: run_sheets_append_row(action, event, steps=steps),
    required=frozenset({"connection_id", "spreadsheet_id", "values"}),
    optional=frozenset({"sheet_name", "sheet_id", "value_input_option"}),
    fields=(
        {"key": "connection_id", "label": "Connection ID", "placeholder": "google", "required": True},
        {"key": "spreadsheet_id", "label": "Spreadsheet ID", "placeholder": "from the sheet URL", "required": True,
         "discover": {"resource": "google-sheets.spreadsheets"},
         "help": "Browse the connection's spreadsheets to pick one"},
        {"key": "sheet_id", "label": "Worksheet ID", "type": "integer", "help": "Numeric gid from the sheet URL; overrides Worksheet name"},
        {"key": "sheet_name", "label": "Worksheet", "placeholder": "todo (default Sheet1)",
         "discover": {"resource": "google-sheets.worksheets",
                      "params": {"spreadsheet_id": "spreadsheet_id"}}},
        {"key": "values", "label": "Row values (JSON)", "type": "json", "required": True,
         "placeholder": '["{trigger.occurred_at|date_format:%Y-%m-%d}", "{text}", "", "NEW"]'},
        {"key": "value_input_option", "label": "Input option", "type": "select",
         "options": ["USER_ENTERED", "RAW"], "default": "USER_ENTERED"},
    ),
))

register(Action(
    type="sheets_find_row",
    label="Google Sheets (find row)",
    icon="sheets",
    description="Find a row by a column's value, optionally create it "
                "(Find-or-create Spreadsheet Row)",
    run=lambda action, event, workflow_id, steps=None: run_sheets_find_row(action, event, steps=steps),
    required=frozenset({"connection_id", "spreadsheet_id", "match_field", "match_value"}),
    optional=frozenset({"sheet_name", "create_if_missing", "values", "value_input_option"}),
    fields=(
        {"key": "connection_id", "label": "Connection ID", "placeholder": "google", "required": True},
        {"key": "spreadsheet_id", "label": "Spreadsheet ID", "placeholder": "from the sheet URL", "required": True,
         "discover": {"resource": "google-sheets.spreadsheets"},
         "help": "Browse the connection's spreadsheets to pick one"},
        {"key": "sheet_name", "label": "Worksheet", "placeholder": "todo (default Sheet1)",
         "discover": {"resource": "google-sheets.worksheets",
                      "params": {"spreadsheet_id": "spreadsheet_id"}}},
        {"key": "match_field", "label": "Match column (header name)", "placeholder": "Task", "required": True,
         "discover": {"resource": "google-sheets.columns",
                      "params": {"spreadsheet_id": "spreadsheet_id", "worksheet": "sheet_name"}},
         "help": "Matched against the header row, trimmed and case-insensitively"},
        {"key": "match_value", "label": "Match value", "placeholder": "{text|replace:/todo :|trim}",
         "required": True,
         "help": "Compared trimmed and case-sensitively against the column's cells"},
        {"key": "create_if_missing", "label": "Create if missing", "type": "boolean",
         "default": "false",
         "help": "Append the row from Row values when nothing matches"},
        {"key": "values", "label": "Row values (JSON)", "type": "textarea",
         "placeholder": '["{text|replace:/todo :|trim}", "NEW"]',
         "help": "Only used when Create if missing is on; same shape as append"},
        {"key": "value_input_option", "label": "Input option", "type": "select",
         "options": ["USER_ENTERED", "RAW"], "default": "USER_ENTERED"},
    ),
))

register(Action(
    type="sheets_lookup_row",
    label="Google Sheets (lookup row)",
    icon="sheets",
    description="Find worksheet rows whose column equals a value "
                "(Lookup Spreadsheet Row). Output: {found, row, values, matches}; "
                "row is the spreadsheet row number (the header is row 1), so it "
                "feeds sheets_update_row directly; a miss is "
                "{found: false, row: null, values: null, matches: []}.",
    run=lambda action, event, workflow_id, steps=None: run_sheets_lookup_row(action, event, steps=steps),
    required=frozenset({"connection_id", "spreadsheet_id", "worksheet", "column", "value"}),
    optional=frozenset({"limit"}),
    fields=(
        {"key": "connection_id", "label": "Connection ID", "placeholder": "google", "required": True},
        {"key": "spreadsheet_id", "label": "Spreadsheet ID", "placeholder": "from the sheet URL", "required": True,
         "discover": {"resource": "google-sheets.spreadsheets"},
         "help": "Browse the connection's spreadsheets to pick one"},
        {"key": "worksheet", "label": "Worksheet", "placeholder": "todo", "required": True,
         "discover": {"resource": "google-sheets.worksheets",
                      "params": {"spreadsheet_id": "spreadsheet_id"}}},
        {"key": "column", "label": "Column", "placeholder": "B or Email", "required": True,
         "discover": {"resource": "google-sheets.columns",
                      "params": {"spreadsheet_id": "spreadsheet_id", "worksheet": "worksheet"}},
         "help": "Column letter (B) or a header name (Email)"},
        {"key": "value", "label": "Value", "placeholder": "{text|replace:/todo :|trim}",
         "required": True,
         "help": "Compared trimmed and case-sensitively against the column's cells"},
        {"key": "limit", "label": "Max matches", "type": "number", "default": "1",
         "help": "How many matching rows ``matches`` may carry (default 1)"},
    ),
))

register(Action(
    type="sheets_update_row",
    label="Google Sheets (update row)",
    icon="sheets",
    description="Overwrite one worksheet row starting at column A "
                "(Update Spreadsheet Row). Pairs with sheets_lookup_row: feed "
                "its row output here. Output: {row: N, updated: true}.",
    run=lambda action, event, workflow_id, steps=None: run_sheets_update_row(action, event, steps=steps),
    required=frozenset({"connection_id", "spreadsheet_id", "worksheet", "row", "values"}),
    optional=frozenset({"value_input_option"}),
    fields=(
        {"key": "connection_id", "label": "Connection ID", "placeholder": "google", "required": True},
        {"key": "spreadsheet_id", "label": "Spreadsheet ID", "placeholder": "from the sheet URL", "required": True,
         "discover": {"resource": "google-sheets.spreadsheets"},
         "help": "Browse the connection's spreadsheets to pick one"},
        {"key": "worksheet", "label": "Worksheet", "placeholder": "todo", "required": True,
         "discover": {"resource": "google-sheets.worksheets",
                      "params": {"spreadsheet_id": "spreadsheet_id"}}},
        {"key": "row", "label": "Row number", "type": "number", "required": True,
         "placeholder": "{steps.lookup.output.row}",
         "discover": {"resource": "google-sheets.rows",
                      "params": {"spreadsheet_id": "spreadsheet_id",
                                 "worksheet": "worksheet"}},
         "help": "Spreadsheet row to overwrite (row 1 is the header)"},
        {"key": "values", "label": "Row values (JSON)", "type": "textarea", "required": True,
         "placeholder": '["{trigger.occurred_at|date_format:%Y-%m-%d}", "{text}", "", "DONE"]'},
        {"key": "value_input_option", "label": "Input option", "type": "select",
         "options": ["USER_ENTERED", "RAW"], "default": "USER_ENTERED"},
    ),
))


register(Action(
    type="sheets_delete_row",
    label="Google Sheets (delete row)",
    icon="sheets",
    description="Delete one worksheet row, shifting the rows under it up "
                "(Delete Spreadsheet Row via batchUpdate). Pairs with "
                "sheets_lookup_row: feed its row output here. Output: "
                "{row: N, deleted: true}.",
    run=lambda action, event, workflow_id, steps=None: run_sheets_delete_row(action, event, steps=steps),
    required=frozenset({"connection_id", "spreadsheet_id", "worksheet", "row"}),
    optional=frozenset(),
    fields=(
        {"key": "connection_id", "label": "Connection ID", "placeholder": "google", "required": True},
        {"key": "spreadsheet_id", "label": "Spreadsheet ID", "placeholder": "from the sheet URL", "required": True,
         "discover": {"resource": "google-sheets.spreadsheets"},
         "help": "Browse the connection's spreadsheets to pick one"},
        {"key": "worksheet", "label": "Worksheet", "placeholder": "todo", "required": True,
         "discover": {"resource": "google-sheets.worksheets",
                      "params": {"spreadsheet_id": "spreadsheet_id"}}},
        {"key": "row", "label": "Row number", "type": "number", "required": True,
         "placeholder": "{steps.lookup.output.row}",
         "discover": {"resource": "google-sheets.rows",
                      "params": {"spreadsheet_id": "spreadsheet_id",
                                 "worksheet": "worksheet"}},
         "help": "Spreadsheet row to delete (row 1 is the header; "
                 "rows below shift up)"},
    ),
))

register(Action(
    type="sheets_clear_values",
    label="Google Sheets (clear values)",
    icon="sheets",
    description="Clear a worksheet or A1 range (Clear Spreadsheet Values via "
                "values:clear) — cell contents go, formatting and the rows "
                "themselves stay. Output: {cleared_range, spreadsheet_id}.",
    run=lambda action, event, workflow_id, steps=None: run_sheets_clear_values(action, event, steps=steps),
    required=frozenset({"connection_id", "spreadsheet_id"}),
    optional=frozenset({"worksheet", "range"}),
    fields=(
        {"key": "connection_id", "label": "Connection ID", "placeholder": "google", "required": True},
        {"key": "spreadsheet_id", "label": "Spreadsheet ID", "placeholder": "from the sheet URL", "required": True,
         "discover": {"resource": "google-sheets.spreadsheets"},
         "help": "Browse the connection's spreadsheets to pick one"},
        {"key": "worksheet", "label": "Worksheet", "placeholder": "todo (default Sheet1)",
         "discover": {"resource": "google-sheets.worksheets",
                      "params": {"spreadsheet_id": "spreadsheet_id"}},
         "help": "The tab to clear — also the sheet prefix when Range "
                 "omits one"},
        {"key": "range", "label": "Range (A1)", "placeholder": "todo!A2:Z100",
         "help": "Optional A1 range — defaults to the whole worksheet; a "
                 "bare range like A2:Z100 is qualified with the worksheet "
                 "name"},
    ),
))

register(Action(
    type="sheets_create_spreadsheet",
    label="Google Sheets (create spreadsheet)",
    icon="sheets",
    description="Create an empty spreadsheet (Create Spreadsheet). Output: "
                "{spreadsheet_id, url, worksheet} — plus headers_applied when "
                "a header row was written — so a follow-up append step can "
                "reference {steps.<id>.output.spreadsheet_id}.",
    run=lambda action, event, workflow_id, steps=None: run_sheets_create_spreadsheet(action, event, steps=steps),
    required=frozenset({"connection_id", "title"}),
    optional=frozenset({"headers"}),
    fields=(
        {"key": "connection_id", "label": "Connection ID", "placeholder": "google", "required": True},
        {"key": "title", "label": "Spreadsheet title", "placeholder": "Weekly sync {date}", "required": True,
         "help": "Rendered from the event like every field"},
        {"key": "headers", "label": "Header row (JSON)", "type": "textarea",
         "placeholder": '["Date", "Task", "Status"]',
         "help": "Optional — written to the new spreadsheet's first worksheet "
                 "at A1; same cell shape as append's values"},
    ),
))

register(Action(
    type="sheets_add_worksheet",
    label="Google Sheets (add worksheet)",
    icon="sheets",
    description="Add one worksheet to a spreadsheet (Create Worksheet via "
                "batchUpdate addSheet). Output: {sheet_id, title, row_count, "
                "column_count, spreadsheet_id} — the title chains into the "
                "values actions' Worksheet fields, the numeric sheet_id is "
                "what batchUpdate addresses. A tab with the same title is "
                "Sheets' 400.",
    run=lambda action, event, workflow_id, steps=None: run_sheets_add_worksheet(
        action, event, steps=steps),
    required=frozenset({"connection_id", "spreadsheet_id", "title"}),
    optional=frozenset({"row_count", "column_count"}),
    fields=(
        {"key": "connection_id", "label": "Connection ID", "placeholder": "google", "required": True},
        {"key": "spreadsheet_id", "label": "Spreadsheet ID", "placeholder": "from the sheet URL", "required": True,
         "discover": {"resource": "google-sheets.spreadsheets"},
         "help": "Browse the connection's spreadsheets to pick one"},
        {"key": "title", "label": "Worksheet title", "placeholder": "Archive {date}", "required": True,
         "help": "Rendered from the event; must not duplicate an existing tab"},
        {"key": "row_count", "label": "Rows", "type": "number", "default": "1000",
         "help": "Grid rows for the new tab (Sheets' default when omitted)"},
        {"key": "column_count", "label": "Columns", "type": "number", "default": "26",
         "help": "Grid columns for the new tab (Sheets' default when omitted)"},
    ),
))


register(Action(
    type="sheets_create_column",
    label="Google Sheets (create column)",
    icon="sheets",
    description="Append one header cell to the worksheet's header row (Create "
                "Spreadsheet Column) — the first free column, or the existing "
                "one when the name is already there. Output: {spreadsheet_id, "
                "worksheet, column, position, cell, created} — position the "
                "1-based column number, cell the A1 address (AA1).",
    run=lambda action, event, workflow_id, steps=None: run_sheets_create_column(
        action, event, steps=steps),
    required=frozenset({"connection_id", "spreadsheet_id", "column"}),
    optional=frozenset({"worksheet"}),
    fields=(
        {"key": "connection_id", "label": "Connection ID", "placeholder": "google", "required": True},
        {"key": "spreadsheet_id", "label": "Spreadsheet ID", "placeholder": "from the sheet URL", "required": True,
         "discover": {"resource": "google-sheets.spreadsheets"},
         "help": "Browse the connection's spreadsheets to pick one"},
        {"key": "worksheet", "label": "Worksheet", "placeholder": "todo (default Sheet1)",
         "discover": {"resource": "google-sheets.worksheets",
                      "params": {"spreadsheet_id": "spreadsheet_id"}}},
        {"key": "column", "label": "Column name (header)", "placeholder": "Status", "required": True,
         "discover": {"resource": "google-sheets.columns",
                      "params": {"spreadsheet_id": "spreadsheet_id", "worksheet": "worksheet"}},
         "help": "The header to add — matched against the header row, trimmed "
                 "and case-insensitively; an existing name is not duplicated"},
    ),
))


def _listed(resource):
    """A discovery runner that lists ``resource`` through the shared layer."""
    def run(connection, params, *, transport=None):
        return provider.discover(connection, resource, params, transport=transport)
    return run


def _tested(connection, *, transport=None):
    return provider.test_connection(connection, transport=transport)


register_discovery(Discovery(
    name="spreadsheets",
    connector="google-sheets",
    label="Spreadsheets",
    description="Spreadsheets the connection can reach, newest first",
    run=_listed("spreadsheets"),
))

register_discovery(Discovery(
    name="worksheets",
    connector="google-sheets",
    label="Worksheets",
    description="Tabs in one spreadsheet",
    params=({"key": "spreadsheet_id", "label": "Spreadsheet ID", "type": "text",
             "required": True},),
    run=_listed("worksheets"),
))

register_discovery(Discovery(
    name="columns",
    connector="google-sheets",
    label="Columns",
    description="Header row of one worksheet",
    params=(
        {"key": "spreadsheet_id", "label": "Spreadsheet ID", "type": "text", "required": True},
        {"key": "worksheet", "label": "Worksheet", "type": "text"},
    ),
    run=_listed("columns"),
))

register_discovery(Discovery(
    name="rows",
    connector="google-sheets",
    label="Rows",
    description="First rows of one worksheet, trailing empty cells trimmed",
    params=(
        {"key": "spreadsheet_id", "label": "Spreadsheet ID", "type": "text", "required": True},
        {"key": "worksheet", "label": "Worksheet", "type": "text"},
    ),
    run=_listed("rows"),
))

register_connection_test(ConnectionTest(connector="google", run=_tested))


# --- trigger discovery: spreadsheet options for the sheets actions' id field ----

from . import trigger_discovery  # noqa: E402
from .trigger_discovery import (  # noqa: E402
    DEFAULT_LIMIT,
    DiscoveryNotFound,
    TriggerDiscovery,
    register_trigger_discovery,
)


def _fetch_spreadsheet_options(event=None, connection_id=None, limit=DEFAULT_LIMIT):
    """Spreadsheet options via the registry listing (first connected Google
    connection when no id is named)."""
    return trigger_discovery.options_from_registry(
        "google-sheets.spreadsheets", connection_id, limit, provider="google",
        option_of=lambda item: {"value": item.get("id"),
                                "label": item.get("name") or item.get("id")})


register_trigger_discovery(TriggerDiscovery(
    connector="google-sheets", label="Google Sheets", kind="options",
    resource="google-sheets.spreadsheets",
    fetch=_fetch_spreadsheet_options))


def _fetch_worksheet_options(event=None, connection_id=None, limit=DEFAULT_LIMIT):
    """Worksheet options for one spreadsheet via the registry listing.

    The TriggerDiscovery fetch signature has no params field, so the
    spreadsheet rides in ``event`` — the same slot the CLI's ``--event``
    and the API body's ``event`` already fill (s3.objects does the same
    with its bucket). A missing spreadsheet is 404, the same convention
    as poll's missing trigger name. The option value is the worksheet's
    title — the sheets actions address tabs by name, not sheetId.
    """
    spreadsheet_id = str(event or "").strip()
    if not spreadsheet_id:
        raise DiscoveryNotFound(
            "spreadsheet_id is required: pass the spreadsheet as 'event'")
    return trigger_discovery.options_from_registry(
        "google-sheets.worksheets", connection_id, limit,
        params={"spreadsheet_id": spreadsheet_id},
        option_of=lambda item: {"value": item.get("name"),
                                "label": item.get("name") or item.get("id")})


register_trigger_discovery(TriggerDiscovery(
    connector="google-sheets", label="Google Sheets", kind="options",
    resource="google-sheets.worksheets",
    fetch=_fetch_worksheet_options))


def _fetch_column_options(event=None, connection_id=None, limit=DEFAULT_LIMIT):
    """Column options for one worksheet via the registry listing.

    The event slot carries ``<spreadsheet_id>`` or
    ``<spreadsheet_id>/<worksheet>`` (trigger_discovery.listing_params);
    without a worksheet the provider defaults to Sheet1. The value is the
    header text — the sheets actions' column fields take header names.
    """
    return trigger_discovery.options_from_registry(
        "google-sheets.columns", connection_id, limit, provider="google",
        params=trigger_discovery.listing_params(event, ("spreadsheet_id", "worksheet")),
        option_of=lambda item: {"value": item.get("name"),
                                "label": item.get("name") or item.get("id")})


register_trigger_discovery(TriggerDiscovery(
    connector="google-sheets", label="Google Sheets", kind="options",
    resource="google-sheets.columns",
    fetch=_fetch_column_options))


def _fetch_row_options(event=None, connection_id=None, limit=DEFAULT_LIMIT):
    """Row options for one worksheet via the registry listing (the same
    event convention as the column options). The value is the spreadsheet
    row number the sheets_update_row / sheets_delete_row row fields store,
    labeled with the row's leading cells."""
    def option_of(item):
        row = str(item.get("row") or item.get("id") or "")
        preview = ", ".join(str(value) for value in (item.get("values") or [])[:4])
        return {"value": row,
                "label": f"Row {row}: {preview}" if preview else f"Row {row}"}

    return trigger_discovery.options_from_registry(
        "google-sheets.rows", connection_id, limit, provider="google",
        params=trigger_discovery.listing_params(event, ("spreadsheet_id", "worksheet")),
        option_of=option_of)


register_trigger_discovery(TriggerDiscovery(
    connector="google-sheets", label="Google Sheets", kind="options",
    resource="google-sheets.rows",
    fetch=_fetch_row_options))


# --- poll sources: "New Spreadsheet Row" / "New or Updated Row" on the ------
# poll-trigger schedule
#
# values.get is JSON, so the generic poll trigger can already read it — but
# rows come back as bare arrays with no ids: nothing to watermark or dedupe
# on. Two sources own the transform (the header row becomes each item's
# column names, the spreadsheet row number the item id) and the seeding
# rule, and publish ``google-sheets``/``row.new`` and ``google-sheets``/
# ``row.updated`` events so workflows match the palette chip while staying
# scoped through the poll-name filter.
#
# ``row.new`` watermarks the row number: rows are append-mostly, so
# "numbered past the cursor" is the news. ``row.updated`` has no feed to
# subscribe to — the values API exposes no per-row modified time — so it
# diffs consecutive listings the way s3.updates reads etags: the cursor
# carries a JSON snapshot of row number → content digest, and a row listed
# before AND now with a changed digest fires. Each source keeps its own
# poll trigger, cursor and seen-set, so both watch the sheet independently.

SHEETS_POLL_ROWS = 1000

# The snapshot shape's version: a foreign or older-shape cursor re-seeds
# rather than diffing against an unreadable baseline.
SHEETS_SNAPSHOT_VERSION = 1


def _sheets_poll_validate(body):
    """Save-time fetch spec: ``spreadsheet_id`` (required), the optional
    ``worksheet`` (default Sheet1), the ``connection_id`` of the Google
    connection to poll as (required — the fetch refreshes its OAuth token),
    plus the fetch defaults every stored sheets poll carries."""
    from ..triggers.email_triggers import TriggerError

    body = body if isinstance(body, dict) else {}
    spreadsheet_id = str(body.get("spreadsheet_id") or "").strip()
    if not spreadsheet_id:
        raise TriggerError(
            "spreadsheet_id is required: name the spreadsheet to watch")
    if not str(body.get("connection_id") or "").strip():
        raise TriggerError(
            "connection_id is required: pick the Google connection to poll as")
    return {
        "spreadsheet_id": spreadsheet_id,
        "worksheet": str(body.get("worksheet") or "").strip(),
        "cursor_mode": "next_cursor",
        "id_path": "row",
        "url": "",
    }


def _sheets_poll_rows(item, *, name, transport=None):
    """The worksheet's data rows for one poll, as ``{"row": <number>,
    "columns": {header: cell}}`` — the shared listing behind both sheets
    sources, fetched through the shared provider helper (``_google_rows``)
    with the bearer token from ``poll_triggers._bearer_token`` so the
    connection's OAuth token is refreshed exactly like the classic fetch.

    Row 1 is the header and never fires; its cells name each row's columns
    (missing cells read as "", headerless columns are dropped, empty rows
    are skipped). Raises ``RuntimeError`` on a failed fetch, like every
    poll source.
    """
    from ..triggers import poll_triggers

    if not str(item.get("connection_id") or "").strip():
        raise RuntimeError(f"poll source '{name}' needs connection_id: "
                           "the Google connection to poll as")
    spreadsheet_id = str(item.get("spreadsheet_id") or "").strip()
    if not spreadsheet_id:
        raise RuntimeError(f"poll source '{name}' needs a stored spreadsheet_id")
    params = {"spreadsheet_id": spreadsheet_id,
              "worksheet": str(item.get("worksheet") or "").strip() or "Sheet1"}
    try:
        rows = provider._google_rows({}, poll_triggers._bearer_token(item["connection_id"]),
                                     params, SHEETS_POLL_ROWS, transport=transport)
    except provider.DiscoveryError as exc:
        raise RuntimeError(f"sheets poll failed: {exc}") from None
    header = [str(cell).strip() for cell in (rows[0]["values"] if rows else [])]
    entries = []
    for row in rows:
        number = row.get("row")
        values = row.get("values") or []
        if not number or number == 1 or not values:
            continue
        columns = {label: (values[index] if index < len(values) else "")
                   for index, label in enumerate(header) if label}
        entries.append({"row": number, "columns": columns})
    return entries


def _sheets_poll_fetch(item, cursor=None, *, transport=None):
    """One poll page as ``(items, next_cursor)`` for ``triggers.poll_sources``.

    Lists the worksheet through ``_sheets_poll_rows``; every row becomes
    ``{"row": <spreadsheet row number>, "id": <same>, <header>: <cell>, ...}``
    — the number under both keys, so it feeds sheets_update_row directly and
    doubles as the event id.

    Seeding: with no stored cursor (first fire) the worksheet's last row
    number (1 on an empty sheet) parks as the watermark and nothing is
    emitted — enabling a trigger must not fire every row already in the
    sheet. With a cursor, only rows numbered strictly past it fire, oldest
    first, and the parked cursor is the last fired row number (the incoming
    one when nothing qualifies; ``fire`` parks it only once the page
    drains).

    The cursor is a position, so rewriting the worksheet's shape shifts it:
    deleting or inserting rows renumbers the rows under them, and a moved
    row can land on a number at or below the cursor and be masked — the
    known polling caveat Zapier's New Spreadsheet Row shares. Raises
    ``RuntimeError`` on a failed fetch, like every poll source.
    """
    items = [{"row": entry["row"], "id": str(entry["row"]), **entry["columns"]}
             for entry in _sheets_poll_rows(item, name="google-sheets.rows",
                                            transport=transport)]
    if cursor is None:
        # First fire: seed the watermark at the worksheet's current depth
        # (row 1 on an empty/header-only sheet) without emitting anything.
        last = max((entry["row"] for entry in items), default=1)
        return [], str(last)
    try:
        boundary = int(str(cursor).strip() or "0")
    except ValueError:
        boundary = 0
    fresh = [entry for entry in items if entry["row"] > boundary]
    return fresh, str(fresh[-1]["row"]) if fresh else str(boundary)


def _sheets_row_digest(entry):
    """The changed marker one worksheet row carries: a digest of its cells.
    The values API exposes no per-row modified time, so an edit is a changed
    digest — s3.updates' etag in its place."""
    payload = json.dumps(entry["columns"], sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode()).hexdigest()


def _sheets_snapshot(entries, item):
    """The worksheet state parked as an updates cursor: a compact JSON map
    of row number → content digest under the watch it was taken under. The
    cursor machinery stores one string, so the diff baseline rides in it the
    way drive's changes cursor rides a page token; at the SHEETS_POLL_ROWS
    cap the row stays far under DynamoDB's item limit."""
    return json.dumps({
        "v": SHEETS_SNAPSHOT_VERSION,
        "spreadsheet_id": str(item.get("spreadsheet_id") or ""),
        "worksheet": str(item.get("worksheet") or ""),
        "rows": {str(entry["row"]): _sheets_row_digest(entry) for entry in entries},
    }, separators=(",", ":"))


def _sheets_parse_snapshot(cursor):
    """The previous snapshot, or None when the cursor is foreign — not JSON,
    another shape or version. None means re-seed: a baseline that cannot be
    read must never be diffed against."""
    try:
        snapshot = json.loads(str(cursor))
    except ValueError:
        return None
    if (not isinstance(snapshot, dict)
            or snapshot.get("v") != SHEETS_SNAPSHOT_VERSION
            or not isinstance(snapshot.get("rows"), dict)):
        return None
    return snapshot


def _sheets_row_updated_validate(body):
    """The rows source's body rules, but the fetch spec's ``id_path`` is
    ``id``: an updated row keeps its spreadsheet row number, so the
    seen-set needs the digest-carrying id to tell a re-edit from an
    already-seen fire."""
    spec = _sheets_poll_validate(body)
    spec["id_path"] = "id"
    return spec


def _sheets_row_updated_fetch(item, cursor=None, *, transport=None):
    """One diff page as ``(items, next_cursor)`` for the updates source.

    Lists the worksheet through the same ``_sheets_poll_rows`` the rows
    source uses and diffs it against the snapshot parked as the cursor. A
    row listed before AND now with a changed digest fires — oldest row
    first, carrying the row's current cells (same shape as a row.new item,
    with ``id`` = ``<row>:<digest>`` so the seen-set recognizes a re-edit).
    Brand-new rows stay the rows source's news and never double-fire here;
    a renumbered row's cells moved with it, so inserting or deleting rows
    can fire the rows under the change — the same position-keyed caveat
    row.new carries.

    Seeding mirrors the rows source: the first fire parks the worksheet's
    current digests and emits nothing. Every ambiguous state re-seeds and
    emits nothing too — a foreign or older-shape cursor, and a stored
    spreadsheet/worksheet that differs from the snapshot's (the old digests
    watched a different sheet). ``fire`` parks the fresh snapshot only once
    the page drains, so a diff bigger than ``max_items`` refetches and the
    seen-set recognizes what already ran. Raises ``RuntimeError`` on a
    failed fetch, like every poll source.
    """
    entries = _sheets_poll_rows(item, name="google-sheets.updates",
                                transport=transport)
    snapshot = _sheets_snapshot(entries, item)
    if cursor is None:
        return [], snapshot
    previous = _sheets_parse_snapshot(cursor)
    watched = previous is not None and all(
        str(previous.get(key) or "") == str(item.get(key) or "")
        for key in ("spreadsheet_id", "worksheet"))
    if not watched:
        return [], snapshot
    rows = previous["rows"]
    changed = []
    for entry in entries:
        digest = _sheets_row_digest(entry)
        old = rows.get(str(entry["row"]))
        if old is not None and old != digest:
            changed.append((entry, digest))
    changed.sort(key=lambda pair: pair[0]["row"])
    items = [{"row": entry["row"], "id": f"{entry['row']}:{digest[:16]}",
              **entry["columns"]} for entry, digest in changed]
    return items, snapshot


def _sheets_poll_view(item):
    """The provider params ``public_view`` shows beside ``source``."""
    return {"spreadsheet_id": item.get("spreadsheet_id"),
            "worksheet": item.get("worksheet")}


register_source(PollSource(
    name="google-sheets.rows", connector="google-sheets", event="row.new",
    label="Google Sheets", validate=_sheets_poll_validate,
    fetch=_sheets_poll_fetch, view=_sheets_poll_view))

register_source(PollSource(
    name="google-sheets.updates", connector="google-sheets", event="row.updated",
    label="Google Sheets updates", validate=_sheets_row_updated_validate,
    fetch=_sheets_row_updated_fetch, view=_sheets_poll_view))


# The event each sheets poll source publishes — the live sample's ask maps
# the stored poll onto it, and the synthetic fallback picks its payload by it.
_SHEETS_SOURCE_EVENTS = {
    "google-sheets.rows": "row.new",
    "google-sheets.updates": "row.updated",
}


def _stored_sheets_poll(name):
    """The stored poll trigger named by ``event`` when it watches a Sheets
    source (rows/updates), or None. A missing selector, unconfigured poll
    triggers, an unknown name and a non-sheets source (the generic poll
    connector owns those) fold together: the caller only distinguishes
    live-vs-fallback, so any storage hiccup folds too — sampling never
    raises for want of infrastructure (see docs/connector-coverage-audit.md)."""
    from ..triggers import poll_triggers

    if not name:
        return None
    try:
        item = poll_triggers.get_item(name)
    except Exception:
        return None
    if not item or str(item.get("source") or "") not in _SHEETS_SOURCE_EVENTS:
        return None
    return item


_SHEETS_SYNTHETIC_ROW = {
    "row": 4,
    "id": "4",
    "Date": "2026-09-28",
    "Invoice": "INV-2026-0042",
    "Amount": "180.00",
}

# row.updated: the same invoice row, an edited Amount and the digest-carrying
# id the updates source emits (``<row>:<digest of the row's cells>``).
_SHEETS_SYNTHETIC_UPDATED_ROW = {
    "row": 4,
    "id": "4:3f5a8c2e91d4b760",
    "Date": "2026-09-28",
    "Invoice": "INV-2026-0042",
    "Amount": "220.00",
}


def _fetch_sheets_sample(event=None, connection_id=None, limit=DEFAULT_LIMIT):
    """The Sheets chip's sample pull: the newest item a stored sheets poll
    watches right now (``source: "live"``), else the newest recorded
    google-sheets run carrying the asked event (``"history"``), else a
    documented example (``"synthetic"``).

    ``event`` names the stored poll trigger; either sheets source qualifies.
    The live pull runs the poll's own fetch once — the rows source against
    the "everything" cursor ``0``, the updates source against the trigger's
    parked snapshot (read, never advanced) — and wraps its newest item in
    the envelope a real fire would publish. A live fetch that cannot run —
    no connection, an unreachable spreadsheet — falls through to the
    recorded/documented sample instead of failing: a sample pull shows the
    payload shape, it never raises. The fallbacks key on the poll's event
    (a ``row.updated`` ask is never answered with a ``row.new`` run or
    example); a bare chip ask without a poll stays on the classic new-row
    sample.
    """
    from ..triggers import poll_triggers

    name = str(event or "").strip().lower()
    item = _stored_sheets_poll(name)
    wanted_event = _SHEETS_SOURCE_EVENTS.get(str((item or {}).get("source") or ""))
    if wanted_event is None and "." in name:
        wanted_event = name  # a dotted ask names an event, not a poll
    if item is not None:
        try:
            if str(item.get("source") or "") == "google-sheets.updates":
                try:
                    seed = poll_triggers.get_cursor(item.get("poll_id"))
                except Exception:
                    seed = None
                items, _next_cursor = _sheets_row_updated_fetch(item, seed)
            else:
                items, _next_cursor = _sheets_poll_fetch(item, "0")
            envelope = (poll_triggers.event_for(item, items[-1])
                        if items else None)
        except Exception:
            envelope = None
        if envelope is not None:
            return {
                "sample": trigger_discovery.as_sample(envelope),
                "source": "live",
                "connection_id": item.get("connection_id") or None,
            }
    found = trigger_discovery.history_sample("google-sheets", event=wanted_event)
    if found is not None:
        return {"sample": found, "source": "history", "connection_id": connection_id}
    if wanted_event == "row.updated":
        synthetic_event, synthetic_data = "row.updated", dict(_SHEETS_SYNTHETIC_UPDATED_ROW)
    else:
        synthetic_event, synthetic_data = "row.new", dict(_SHEETS_SYNTHETIC_ROW)
    return {
        "sample": trigger_discovery.synthetic_sample(
            "google-sheets", synthetic_event, dict(synthetic_data)),
        "source": "synthetic",
        "connection_id": connection_id,
    }


register_trigger_discovery(TriggerDiscovery(
    connector="google-sheets", label="Google Sheets", kind="sample", resource="",
    fetch=_fetch_sheets_sample))
