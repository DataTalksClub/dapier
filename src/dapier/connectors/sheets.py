"""Google Sheets connector: append rows through a Google connection."""
from ..engine.actions.sheets import run_sheets_append_row
from .registry import Action, register

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
        {"key": "spreadsheet_id", "label": "Spreadsheet ID", "placeholder": "from the sheet URL", "required": True},
        {"key": "sheet_name", "label": "Worksheet", "placeholder": "todo (default Sheet1)"},
        {"key": "values", "label": "Row values (JSON)", "type": "textarea", "required": True,
         "placeholder": '["{trigger.occurred_at|date_format:%Y-%m-%d}", "{text}", "", "NEW"]'},
        {"key": "value_input_option", "label": "Input option", "type": "select",
         "options": ["USER_ENTERED", "RAW"], "default": "USER_ENTERED"},
    ),
))
