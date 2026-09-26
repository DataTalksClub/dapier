"""sheets_append_row action: append rows through a Google connection."""
import json
import urllib.parse

from ...connections import tokens
from . import base
from .templating import render

VALUE_INPUT_OPTIONS = ("USER_ENTERED", "RAW")


def _sheets_connection(connection_id):
    return base._connected_connection(connection_id)


def _rows_from_action(action, event, steps=None):
    """The action's ``values`` as row arrays with every cell template-rendered.

    ``values`` is a JSON array of row arrays (a flat array counts as one row)
    and may arrive as a JSON string from a raw-text field. Cells render per
    leaf string after parsing, so task text containing quotes stays intact.
    """
    raw = action.get("values")
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError:
            raise ValueError("values must be a JSON array of row arrays")
    if not isinstance(raw, list) or not raw:
        raise ValueError("values must be a non-empty JSON array of row arrays")
    if all(not isinstance(row, list) for row in raw):
        raw = [raw]
    elif not all(isinstance(row, list) for row in raw):
        raise ValueError("values rows must all be arrays")
    rows = []
    for row in raw:
        cells = []
        for cell in row:
            cells.append(render(cell, event, steps) if isinstance(cell, str) else cell)
        rows.append(cells)
    return rows


def _append_rows(access_token, spreadsheet_id, sheet_name, rows, value_input_option,
                 *, transport=None):
    transport = transport or base._default_transport
    url = (
        "https://sheets.googleapis.com/v4/spreadsheets/"
        f"{urllib.parse.quote(spreadsheet_id, safe='')}/values/"
        f"{urllib.parse.quote(sheet_name, safe='')}:append"
        f"?valueInputOption={urllib.parse.quote(value_input_option)}"
        "&insertDataOption=INSERT_ROWS"
    )
    headers = {
        "authorization": f"Bearer {access_token}",
        "content-type": "application/json",
    }
    try:
        status, response = transport(
            "POST", url, headers=headers,
            body=json.dumps({"values": rows}).encode(), timeout=15,
        )
    except Exception as exc:
        raise RuntimeError(f"google sheets unreachable: {type(exc).__name__}")
    if status >= 300:
        detail = ""
        try:
            error = json.loads(response.decode() or "{}").get("error")
            if isinstance(error, dict) and error.get("message"):
                detail = f" ({str(error['message'])[:200]})"
        except (ValueError, UnicodeDecodeError):
            pass
        raise RuntimeError(f"google sheets append returned HTTP {status}{detail}")
    try:
        result = json.loads(response.decode() or "{}")
    except (ValueError, UnicodeDecodeError):
        return {}
    return result if isinstance(result, dict) else {}


def run_sheets_append_row(action, event, *, transport=None, steps=None):
    """Append rows to a worksheet via the Sheets API ``values.append``.

    The range is the worksheet name, so rows land after the sheet's last row
    of data — the Create Spreadsheet Row behavior. ``USER_ENTERED`` (the
    default) lets Sheets parse dates and numbers; ``RAW`` stores text as-is.
    """
    connection = _sheets_connection(action["connection_id"])
    access_token, _info = tokens.get_access_token(connection, transport=transport)
    spreadsheet_id = str(action.get("spreadsheet_id") or "").strip()
    if not spreadsheet_id:
        raise ValueError("sheets_append_row requires a spreadsheet_id")
    sheet_name = str(action.get("sheet_name") or "").strip() or "Sheet1"
    option = str(action.get("value_input_option") or "USER_ENTERED").strip().upper()
    if option not in VALUE_INPUT_OPTIONS:
        raise ValueError("value_input_option must be one of: USER_ENTERED, RAW")
    rows = _rows_from_action(action, event, steps)
    result = _append_rows(access_token, spreadsheet_id, sheet_name, rows, option,
                          transport=transport)
    updates = result.get("updates") if isinstance(result.get("updates"), dict) else {}
    return {
        "spreadsheet_id": updates.get("spreadsheetId") or spreadsheet_id,
        "updated_range": updates.get("updatedRange"),
        "updated_rows": updates.get("updatedRows"),
        "updated_cells": updates.get("updatedCells"),
    }
