"""Google Sheets connector: append rows through a Google connection, plus
the google-sheets discovery resources and the Google identity check.

The Discovery/ConnectionTest run callables registered here delegate to the
shared provider layer (``connections.discovery``) — the same code the
/api/*/connections/.../discover endpoints serve — so the catalog metadata
and the live listings can never drift apart.
"""
from ..connections import discovery as provider
from ..engine.actions.sheets import (
    run_sheets_append_row,
    run_sheets_find_row,
    run_sheets_lookup_row,
    run_sheets_update_row,
)
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
