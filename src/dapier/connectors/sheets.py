"""Google Sheets connector: append rows through a Google connection, plus
the google-sheets discovery resources and the Google identity check.

The Discovery/ConnectionTest run callables registered here delegate to the
shared provider layer (``connections.discovery``) — the same code the
/api/*/connections/.../discover endpoints serve — so the catalog metadata
and the live listings can never drift apart.
"""
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
    optional=frozenset({"sheet_name", "value_input_option"}),
    fields=(
        {"key": "connection_id", "label": "Connection ID", "placeholder": "google", "required": True},
        {"key": "spreadsheet_id", "label": "Spreadsheet ID", "placeholder": "from the sheet URL", "required": True,
         "discover": {"resource": "google-sheets.spreadsheets"},
         "help": "Browse the connection's spreadsheets to pick one"},
        {"key": "sheet_name", "label": "Worksheet", "placeholder": "todo (default Sheet1)",
         "discover": {"resource": "google-sheets.worksheets",
                      "params": {"spreadsheet_id": "spreadsheet_id"}}},
        {"key": "values", "label": "Row values (JSON)", "type": "textarea", "required": True,
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


# --- poll source: "New Spreadsheet Row" on the poll-trigger schedule --------
#
# values.get is JSON, so the generic poll trigger can already read it — but
# rows come back as bare arrays with no ids: nothing to watermark or dedupe
# on. The ``google-sheets.rows`` source owns the transform (the header row
# becomes each item's column names, and the spreadsheet row number is the
# item id) and the seeding rule, and publishes ``google-sheets``/``row.new``
# events so workflows match the palette chip while staying scoped through
# the poll-name filter.

SHEETS_POLL_ROWS = 1000


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


def _sheets_poll_fetch(item, cursor=None, *, transport=None):
    """One poll page as ``(items, next_cursor)`` for ``triggers.poll_sources``.

    Polls values.get through the shared provider helper (``_google_rows``),
    with the bearer token from ``poll_triggers._bearer_token`` so the
    connection's OAuth token is refreshed exactly like the classic fetch.
    Row 1 is the header and never fires; its cells name each item's columns
    (missing cells read as "", headerless columns are dropped). Every other
    row becomes ``{"row": <spreadsheet row number>, "id": <same>,
    <header>: <cell>, ...}`` — the number under both keys, so it feeds
    sheets_update_row directly and doubles as the event id.

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
    from ..triggers import poll_triggers

    if not str(item.get("connection_id") or "").strip():
        raise RuntimeError("poll source 'google-sheets.rows' needs connection_id: "
                           "the Google connection to poll as")
    spreadsheet_id = str(item.get("spreadsheet_id") or "").strip()
    if not spreadsheet_id:
        raise RuntimeError("poll source 'google-sheets.rows' needs a stored spreadsheet_id")
    params = {"spreadsheet_id": spreadsheet_id,
              "worksheet": str(item.get("worksheet") or "").strip() or "Sheet1"}
    try:
        rows = provider._google_rows({}, poll_triggers._bearer_token(item["connection_id"]),
                                     params, SHEETS_POLL_ROWS, transport=transport)
    except provider.DiscoveryError as exc:
        raise RuntimeError(f"sheets poll failed: {exc}") from None
    header = [str(cell).strip() for cell in (rows[0]["values"] if rows else [])]
    items = []
    for row in rows:
        number = row.get("row")
        values = row.get("values") or []
        if not number or number == 1 or not values:
            continue
        columns = {name: (values[index] if index < len(values) else "")
                   for index, name in enumerate(header) if name}
        items.append({"row": number, "id": str(number), **columns})
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


def _sheets_poll_view(item):
    """The provider params ``public_view`` shows beside ``source``."""
    return {"spreadsheet_id": item.get("spreadsheet_id"),
            "worksheet": item.get("worksheet")}


register_source(PollSource(
    name="google-sheets.rows", connector="google-sheets", event="row.new",
    label="Google Sheets", validate=_sheets_poll_validate,
    fetch=_sheets_poll_fetch, view=_sheets_poll_view))


def _stored_sheets_poll(name):
    """The stored poll trigger named by ``event`` when it watches Sheets, or
    None. A missing selector, unconfigured poll triggers, an unknown name
    and a non-sheets source (the generic poll connector owns those) fold
    together: the caller only distinguishes live-vs-fallback, so any
    storage hiccup folds too — sampling never raises for want of
    infrastructure (see docs/connector-coverage-audit.md)."""
    from ..triggers import poll_triggers

    if not name:
        return None
    try:
        item = poll_triggers.get_item(name)
    except Exception:
        return None
    if not item or str(item.get("source") or "") != "google-sheets.rows":
        return None
    return item


_SHEETS_SYNTHETIC_ROW = {
    "row": 4,
    "id": "4",
    "Date": "2026-09-28",
    "Invoice": "INV-2026-0042",
    "Amount": "180.00",
}


def _fetch_sheets_sample(event=None, connection_id=None, limit=DEFAULT_LIMIT):
    """The Sheets chip's sample pull: the newest row a stored sheets poll
    watches right now (``source: "live"``), else the newest recorded
    google-sheets run (``"history"``), else a documented invoice row
    (``"synthetic"``).

    ``event`` names the stored poll trigger; only ``google-sheets.rows``
    polls qualify. The live pull runs the poll's own fetch once against the
    "everything" cursor ``0`` — no stored cursor is read or advanced — and
    wraps its newest row in the envelope a real fire would publish. A live
    fetch that cannot run — no connection, an unreachable spreadsheet —
    falls through to the recorded/documented sample instead of failing: a
    sample pull shows the payload shape, it never raises.
    """
    from ..triggers import poll_triggers

    name = str(event or "").strip().lower()
    item = _stored_sheets_poll(name)
    if item is not None:
        try:
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
    found = trigger_discovery.history_sample("google-sheets")
    if found is not None:
        return {"sample": found, "source": "history", "connection_id": connection_id}
    return {
        "sample": trigger_discovery.synthetic_sample(
            "google-sheets", "row.new", dict(_SHEETS_SYNTHETIC_ROW)),
        "source": "synthetic",
        "connection_id": connection_id,
    }


register_trigger_discovery(TriggerDiscovery(
    connector="google-sheets", label="Google Sheets", kind="sample", resource="",
    fetch=_fetch_sheets_sample))
