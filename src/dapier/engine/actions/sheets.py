"""sheets_append_row / sheets_find_row actions: Google Sheets through a
Google connection."""
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


def _fetch_sheet_rows(access_token, spreadsheet_id, sheet_name, *, transport=None):
    """Header plus every data row of the worksheet, from one GET.

    The A1 range is quoted whole and late — quoting the worksheet name first
    would double-encode spaces, and leaving "!" safe breaks Sheets' parser.
    """
    transport = transport or base._default_transport
    range_a1 = urllib.parse.quote(f"{sheet_name}!A1:ZZ10000", safe="")
    url = (
        "https://sheets.googleapis.com/v4/spreadsheets/"
        f"{urllib.parse.quote(spreadsheet_id, safe='')}/values/{range_a1}"
    )
    headers = {"authorization": f"Bearer {access_token}"}
    try:
        status, response = transport("GET", url, headers=headers, body=None, timeout=15)
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
        raise RuntimeError(f"google sheets read returned HTTP {status}{detail}")
    try:
        result = json.loads(response.decode() or "{}")
    except (ValueError, UnicodeDecodeError):
        return []
    values = result.get("values") if isinstance(result, dict) else None
    return values if isinstance(values, list) else []


def _rendered_field(action, key, event, steps=None):
    value = action.get(key)
    if isinstance(value, str):
        value = render(value, event, steps)
    return str(value or "").strip()


def _create_rows(action, event, steps=None):
    """``values`` as rendered row arrays for create-if-missing.

    The missing/empty case reports the find-specific message; malformed
    shapes keep ``_rows_from_action``'s own diagnostics.
    """
    raw = action.get("values")
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        raise ValueError("sheets_find_row needs values to create the missing row")
    try:
        return _rows_from_action(action, event, steps)
    except ValueError as exc:
        if "non-empty JSON array" in str(exc):
            raise ValueError(
                "sheets_find_row needs values to create the missing row") from exc
        raise


def _flag(action, key):
    """Designer boolean fields arrive as "true"/"false" strings."""
    value = action.get(key)
    if isinstance(value, str):
        return value.strip().lower() in ("true", "1", "yes", "on")
    return bool(value)


_NOT_FOUND = {"found": False, "row_number": None, "row": None, "values": None}


def run_sheets_find_row(action, event, *, transport=None, steps=None):
    """Find the first worksheet row whose cell under ``match_field`` equals
    ``match_value`` (Zapier's Find-or-create semantics).

    ``match_field`` is matched against a header cell trimmed and
    case-insensitively; ``match_value`` is compared trimmed and
    case-sensitively. No match is a ``found: False`` result, not an error.
    With ``create_if_missing`` the missing row is appended from ``values``
    (same shape as :func:`run_sheets_append_row`) and the output reports
    ``created: True``.
    """
    connection = _sheets_connection(action["connection_id"])
    access_token, _info = tokens.get_access_token(connection, transport=transport)
    spreadsheet_id = _rendered_field(action, "spreadsheet_id", event, steps)
    if not spreadsheet_id:
        raise ValueError("sheets_find_row requires a spreadsheet_id")
    sheet_name = str(action.get("sheet_name") or "").strip() or "Sheet1"
    match_field = _rendered_field(action, "match_field", event, steps)
    if not match_field:
        raise ValueError("sheets_find_row requires a match_field")
    match_value = _rendered_field(action, "match_value", event, steps)
    if not match_value:
        raise ValueError("sheets_find_row requires a match_value")

    rows = _fetch_sheet_rows(access_token, spreadsheet_id, sheet_name,
                             transport=transport)
    headers = ([str(cell).strip() for cell in rows[0]]
               if rows and isinstance(rows[0], list) else [])
    field_index = next(
        (index for index, header in enumerate(headers)
         if header.lower() == match_field.lower()), None)
    if field_index is not None:
        for offset, row in enumerate(rows[1:]):
            cells = row if isinstance(row, list) else []
            if field_index < len(cells) and str(cells[field_index]).strip() == match_value:
                row_dict = {
                    header: (cells[index] if index < len(cells) else "")
                    for index, header in enumerate(headers)
                }
                return {"found": True, "row_number": offset + 1,
                        "row": row_dict, "values": cells}

    if not _flag(action, "create_if_missing"):
        return dict(_NOT_FOUND)
    create_rows = _create_rows(action, event, steps)
    option = str(action.get("value_input_option") or "USER_ENTERED").strip().upper()
    if option not in VALUE_INPUT_OPTIONS:
        raise ValueError("value_input_option must be one of: USER_ENTERED, RAW")
    result = _append_rows(access_token, spreadsheet_id, sheet_name, create_rows,
                          option, transport=transport)
    updates = result.get("updates") if isinstance(result.get("updates"), dict) else {}
    return {
        "found": False,
        "created": True,
        "row_number": None,
        "row": None,
        "values": create_rows[0],
        "updated_range": updates.get("updatedRange"),
        "updated_cells": updates.get("updatedCells"),
    }


def _error_detail(raw):
    """The Sheets API error message from a response body, for surfacing."""
    try:
        error = json.loads(raw.decode() or "{}").get("error")
        if isinstance(error, dict) and error.get("message"):
            return f" ({str(error['message'])[:200]})"
    except (ValueError, UnicodeDecodeError):
        pass
    return ""


def _column_index(column):
    """A 1-2 letter A1 column ("B", "aa") as a 0-based index, else None."""
    letters = str(column).strip().upper()
    if not letters or len(letters) > 2 or not all("A" <= char <= "Z" for char in letters):
        return None
    index = 0
    for char in letters:
        index = index * 26 + (ord(char) - ord("A") + 1)
    return index - 1


def run_sheets_lookup_row(action, event, *, transport=None, steps=None):
    """Find worksheet rows whose ``column`` cell equals ``value``.

    ``column`` is a column letter ("B") or a header name ("Email" — matched
    against the header row trimmed and case-insensitively, like
    :func:`run_sheets_find_row`'s match_field); ``value`` is rendered from the
    event and compared trimmed, case-sensitively. Row numbers count the header
    as row 1, so they feed :func:`run_sheets_update_row`'s ``row`` directly.
    No match is a ``found: False`` result, not an error; ``matches`` carries
    up to ``limit`` (default 1) ``{"row": N, "values": [...]}`` entries.
    """
    connection = _sheets_connection(action["connection_id"])
    access_token, _info = tokens.get_access_token(connection, transport=transport)
    spreadsheet_id = _rendered_field(action, "spreadsheet_id", event, steps)
    if not spreadsheet_id:
        raise ValueError("sheets_lookup_row requires a spreadsheet_id")
    worksheet = str(action.get("worksheet") or "").strip() or "Sheet1"
    column = _rendered_field(action, "column", event, steps)
    if not column:
        raise ValueError("sheets_lookup_row requires a column")
    value = _rendered_field(action, "value", event, steps)
    if not value:
        raise ValueError("sheets_lookup_row requires a value")
    raw_limit = _rendered_field(action, "limit", event, steps)
    try:
        limit = int(raw_limit) if raw_limit else 1
    except ValueError:
        raise ValueError("sheets_lookup_row limit must be a whole number")
    if limit < 1:
        raise ValueError("sheets_lookup_row limit must be at least 1")

    rows = _fetch_sheet_rows(access_token, spreadsheet_id, worksheet,
                             transport=transport)
    headers = ([str(cell).strip() for cell in rows[0]]
               if rows and isinstance(rows[0], list) else [])
    column_index = _column_index(column)
    if column_index is None:
        column_index = next(
            (index for index, header in enumerate(headers)
             if header.lower() == column.lower()), None)
    if column_index is None:
        raise ValueError(
            f"sheets_lookup_row: no column {column!r} in the {worksheet!r} header row")

    matches = []
    for offset, row in enumerate(rows[1:]):
        cells = row if isinstance(row, list) else []
        if column_index < len(cells) and str(cells[column_index]).strip() == value:
            matches.append({"row": offset + 2, "values": cells})
            if len(matches) >= limit:
                break
    if not matches:
        return {"found": False, "row": None, "values": None, "matches": []}
    return {"found": True, "row": matches[0]["row"],
            "values": matches[0]["values"], "matches": matches}


def _update_row(access_token, spreadsheet_id, sheet_name, row_number, cells,
                value_input_option, *, transport=None):
    """Overwrite one worksheet row starting at column A (``values.update``)."""
    transport = transport or base._default_transport
    range_a1 = urllib.parse.quote(f"{sheet_name}!A{row_number}", safe="")
    url = (
        "https://sheets.googleapis.com/v4/spreadsheets/"
        f"{urllib.parse.quote(spreadsheet_id, safe='')}/values/{range_a1}"
        f"?valueInputOption={urllib.parse.quote(value_input_option)}"
    )
    headers = {
        "authorization": f"Bearer {access_token}",
        "content-type": "application/json",
    }
    try:
        status, response = transport(
            "PUT", url, headers=headers,
            body=json.dumps({"values": [cells]}).encode(), timeout=15,
        )
    except Exception as exc:
        raise RuntimeError(f"google sheets unreachable: {type(exc).__name__}")
    if status >= 300:
        raise RuntimeError(
            f"google sheets update returned HTTP {status}{_error_detail(response)}")


def run_sheets_update_row(action, event, *, transport=None, steps=None):
    """Overwrite one worksheet row starting at column A.

    ``row`` is the spreadsheet row number (the header is row 1 —
    :func:`run_sheets_lookup_row`'s ``row`` output feeds it directly);
    ``values`` is one row of cells, rendered per cell like
    :func:`run_sheets_append_row`. Output: ``{"row": N, "updated": True}``.
    """
    connection = _sheets_connection(action["connection_id"])
    access_token, _info = tokens.get_access_token(connection, transport=transport)
    spreadsheet_id = _rendered_field(action, "spreadsheet_id", event, steps)
    if not spreadsheet_id:
        raise ValueError("sheets_update_row requires a spreadsheet_id")
    worksheet = str(action.get("worksheet") or "").strip() or "Sheet1"
    row_number = _rendered_field(action, "row", event, steps)
    try:
        row_number = int(row_number)
    except ValueError:
        raise ValueError("sheets_update_row requires a whole-number row")
    if row_number < 1:
        raise ValueError("sheets_update_row row must be at least 1")
    option = str(action.get("value_input_option") or "USER_ENTERED").strip().upper()
    if option not in VALUE_INPUT_OPTIONS:
        raise ValueError("value_input_option must be one of: USER_ENTERED, RAW")
    rows = _rows_from_action(action, event, steps)
    if len(rows) != 1:
        raise ValueError(
            "sheets_update_row updates one row; values must be a single row of cells")
    _update_row(access_token, spreadsheet_id, worksheet, row_number, rows[0],
                option, transport=transport)
    return {"row": row_number, "updated": True}
