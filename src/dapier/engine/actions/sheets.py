"""sheets_append_row / sheets_find_row actions: Google Sheets through a
Google connection (plus the update/lookup/delete/create/clear staples and
the header-column writer)."""
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


def _sheet_title(access_token, spreadsheet_id, sheet_id, *, transport=None):
    transport = transport or base._default_transport
    url = ("https://sheets.googleapis.com/v4/spreadsheets/"
           f"{urllib.parse.quote(spreadsheet_id, safe='')}?fields=sheets.properties")
    status, response = transport("GET", url, headers={"authorization": f"Bearer {access_token}"},
                                 body=None, timeout=15)
    if status >= 300:
        raise RuntimeError(f"google sheets metadata returned HTTP {status}")
    for sheet in json.loads(response).get("sheets", []):
        props = sheet.get("properties", {})
        if str(props.get("sheetId")) == sheet_id:
            # Quote the title as an A1 range, so spaces, quotes and named ranges cannot alter selection.
            return "'" + props["title"].replace("'", "''") + "'"
    raise ValueError(f"worksheet id {sheet_id} does not exist in spreadsheet")


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
    if "sheet_id" in action:
        sheet_id = render(str(action["sheet_id"]), event, steps).strip()
        if not sheet_id.isdigit():
            raise ValueError("sheets_append_row sheet_id must be a non-negative integer")
        sheet_name = _sheet_title(access_token, spreadsheet_id, sheet_id, transport=transport)
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


def _update_range(access_token, spreadsheet_id, range_a1, cells,
                  value_input_option, *, transport=None):
    """Overwrite the cells at one A1 range (``values.update``)."""
    transport = transport or base._default_transport
    range_quoted = urllib.parse.quote(range_a1, safe="")
    url = (
        "https://sheets.googleapis.com/v4/spreadsheets/"
        f"{urllib.parse.quote(spreadsheet_id, safe='')}/values/{range_quoted}"
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


def _update_row(access_token, spreadsheet_id, sheet_name, row_number, cells,
                value_input_option, *, transport=None):
    """Overwrite one worksheet row starting at column A (``values.update``)."""
    _update_range(access_token, spreadsheet_id, f"{sheet_name}!A{row_number}",
                  cells, value_input_option, transport=transport)


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


def _sheet_id_for_title(access_token, spreadsheet_id, sheet_name, *, transport=None):
    """The numeric sheetId of the worksheet titled ``sheet_name``.

    ``batchUpdate`` addresses a worksheet by its numeric sheetId, not its
    name, so a delete has to resolve the title against the spreadsheet's
    sheet list first (one GET, properties only).
    """
    transport = transport or base._default_transport
    url = (
        "https://sheets.googleapis.com/v4/spreadsheets/"
        f"{urllib.parse.quote(spreadsheet_id, safe='')}"
        "?fields=sheets(properties(sheetId,title))"
    )
    headers = {"authorization": f"Bearer {access_token}"}
    try:
        status, response = transport("GET", url, headers=headers, body=None, timeout=15)
    except Exception as exc:
        raise RuntimeError(f"google sheets unreachable: {type(exc).__name__}")
    if status >= 300:
        raise RuntimeError(
            f"google sheets read returned HTTP {status}{_error_detail(response)}")
    try:
        result = json.loads(response.decode() or "{}")
    except (ValueError, UnicodeDecodeError):
        raise RuntimeError("google sheets returned an unreadable body") from None
    for sheet in result.get("sheets") or []:
        properties = sheet.get("properties") if isinstance(sheet, dict) else None
        if isinstance(properties, dict) and str(properties.get("title") or "") == sheet_name:
            return properties.get("sheetId")
    raise ValueError(
        f"sheets_delete_row: no worksheet named {sheet_name!r} in the spreadsheet")


def _delete_row(access_token, spreadsheet_id, sheet_id, row_number, *, transport=None):
    """Remove one row (``batchUpdate`` ``deleteDimension``), shifting the rows
    under it up; the 1-based spreadsheet row number becomes a 0-based index
    range, the same numbering :func:`run_sheets_update_row` writes at."""
    transport = transport or base._default_transport
    url = (
        "https://sheets.googleapis.com/v4/spreadsheets/"
        f"{urllib.parse.quote(spreadsheet_id, safe='')}:batchUpdate"
    )
    headers = {
        "authorization": f"Bearer {access_token}",
        "content-type": "application/json",
    }
    payload = {"requests": [{"deleteDimension": {"range": {
        "sheetId": sheet_id, "dimension": "ROWS",
        "startIndex": row_number - 1, "endIndex": row_number,
    }}}]}
    try:
        status, response = transport(
            "POST", url, headers=headers, body=json.dumps(payload).encode(), timeout=15,
        )
    except Exception as exc:
        raise RuntimeError(f"google sheets unreachable: {type(exc).__name__}")
    if status >= 300:
        raise RuntimeError(
            f"google sheets delete returned HTTP {status}{_error_detail(response)}")


def run_sheets_delete_row(action, event, *, transport=None, steps=None):
    """Delete one worksheet row (Sheets API ``batchUpdate`` deleteDimension).

    ``row`` is the spreadsheet row number (the header is row 1 —
    :func:`run_sheets_lookup_row`'s ``row`` output feeds it directly, like
    :func:`run_sheets_update_row`). The worksheet's numeric sheetId is
    resolved from its title first, because deleteDimension addresses sheets
    by id. Output: ``{"row": N, "deleted": True}``.
    """
    connection = _sheets_connection(action["connection_id"])
    access_token, _info = tokens.get_access_token(connection, transport=transport)
    spreadsheet_id = _rendered_field(action, "spreadsheet_id", event, steps)
    if not spreadsheet_id:
        raise ValueError("sheets_delete_row requires a spreadsheet_id")
    worksheet = str(action.get("worksheet") or "").strip() or "Sheet1"
    row_number = _rendered_field(action, "row", event, steps)
    try:
        row_number = int(row_number)
    except ValueError:
        raise ValueError("sheets_delete_row requires a whole-number row")
    if row_number < 1:
        raise ValueError("sheets_delete_row row must be at least 1")
    sheet_id = _sheet_id_for_title(access_token, spreadsheet_id, worksheet,
                                   transport=transport)
    _delete_row(access_token, spreadsheet_id, sheet_id, row_number,
                transport=transport)
    return {"row": row_number, "deleted": True}


def _create_spreadsheet(access_token, title, *, transport=None):
    """Create an empty spreadsheet (``spreadsheets.create``) and return its
    full resource — the response carries spreadsheetId, spreadsheetUrl and
    the first worksheet's title."""
    transport = transport or base._default_transport
    url = "https://sheets.googleapis.com/v4/spreadsheets"
    headers = {
        "authorization": f"Bearer {access_token}",
        "content-type": "application/json",
    }
    try:
        status, response = transport(
            "POST", url, headers=headers,
            body=json.dumps({"properties": {"title": title}}).encode(), timeout=15,
        )
    except Exception as exc:
        raise RuntimeError(f"google sheets unreachable: {type(exc).__name__}")
    if status >= 300:
        raise RuntimeError(
            f"google sheets create returned HTTP {status}{_error_detail(response)}")
    try:
        result = json.loads(response.decode() or "{}")
    except (ValueError, UnicodeDecodeError):
        raise RuntimeError("google sheets create returned an unreadable body") from None
    return result if isinstance(result, dict) else {}


def run_sheets_create_spreadsheet(action, event, *, transport=None, steps=None):
    """Create an empty spreadsheet (Sheets API ``spreadsheets.create``).

    ``title`` renders against the event; the optional ``headers`` is one row
    of cells (same shape as append's values) written to the new
    spreadsheet's first worksheet at A1. Output:
    ``{spreadsheet_id, url, worksheet}`` — plus ``headers_applied: True``
    when headers were written — so a follow-up
    :func:`run_sheets_append_row` can reference the spreadsheet it created.
    """
    connection = _sheets_connection(action["connection_id"])
    access_token, _info = tokens.get_access_token(connection, transport=transport)
    title = _rendered_field(action, "title", event, steps)
    if not title:
        raise ValueError("sheets_create_spreadsheet requires a title")
    created = _create_spreadsheet(access_token, title, transport=transport)
    spreadsheet_id = str(created.get("spreadsheetId") or "")
    if not spreadsheet_id:
        raise RuntimeError("google sheets create returned no spreadsheetId")
    sheets = created.get("sheets") if isinstance(created.get("sheets"), list) else []
    properties = sheets[0].get("properties") if sheets and isinstance(sheets[0], dict) else {}
    worksheet = str((properties or {}).get("title") or "Sheet1")
    output = {
        "spreadsheet_id": spreadsheet_id,
        "url": created.get("spreadsheetUrl"),
        "worksheet": worksheet,
    }
    raw_headers = action.get("headers")
    if raw_headers is None or (isinstance(raw_headers, str) and not raw_headers.strip()):
        return output
    rows = _rows_from_action({"values": raw_headers}, event, steps)
    if len(rows) != 1:
        raise ValueError(
            "sheets_create_spreadsheet headers must be a single row of cells")
    _update_row(access_token, spreadsheet_id, worksheet, 1, rows[0],
                "USER_ENTERED", transport=transport)
    output["headers_applied"] = True
    return output


# --- sheets_clear_values: one worksheet or A1 range, wiped ----------------------


def _clear_range(access_token, spreadsheet_id, range_a1, *, transport=None):
    """Clear one A1 range (``values:clear``); returns the parsed response —
    the API answers with the spreadsheetId and the range it actually cleared
    (the whole-sheet bound, in grid terms)."""
    transport = transport or base._default_transport
    url = (
        "https://sheets.googleapis.com/v4/spreadsheets/"
        f"{urllib.parse.quote(spreadsheet_id, safe='')}/values/"
        f"{urllib.parse.quote(range_a1, safe='')}:clear"
    )
    headers = {"authorization": f"Bearer {access_token}"}
    try:
        status, response = transport(
            "POST", url, headers=headers, body=None, timeout=15,
        )
    except Exception as exc:
        raise RuntimeError(f"google sheets unreachable: {type(exc).__name__}")
    if status >= 300:
        raise RuntimeError(
            f"google sheets clear returned HTTP {status}{_error_detail(response)}")
    try:
        result = json.loads(response.decode() or "{}")
    except (ValueError, UnicodeDecodeError):
        return {}
    return result if isinstance(result, dict) else {}


def run_sheets_clear_values(action, event, *, transport=None, steps=None):
    """Clear a worksheet's values (Sheets API ``values:clear``; Zapier's
    clear semantics — cell contents go, formatting and the rows themselves
    stay, so the sheet's shape is untouched).

    ``worksheet`` names the tab (default Sheet1, like every sheets action)
    and the optional ``range`` narrows the clear in A1 notation. A range
    without a sheet prefix ("A2:Z100") is qualified with the worksheet
    title, exactly the way :func:`_fetch_sheet_rows` builds the lookup's
    whole-sheet range; the default with no ``range`` is the worksheet's
    full data area (``{worksheet}!A1:ZZ10000``). Output: ``{cleared_range,
    spreadsheet_id}`` from the API's response.
    """
    connection = _sheets_connection(action["connection_id"])
    access_token, _info = tokens.get_access_token(connection, transport=transport)
    spreadsheet_id = _rendered_field(action, "spreadsheet_id", event, steps)
    if not spreadsheet_id:
        raise ValueError("sheets_clear_values requires a spreadsheet_id")
    worksheet = str(action.get("worksheet") or "").strip() or "Sheet1"
    range_a1 = _rendered_field(action, "range", event, steps)
    if range_a1:
        if "!" not in range_a1:
            range_a1 = f"{worksheet}!{range_a1}"
    else:
        range_a1 = f"{worksheet}!A1:ZZ10000"
    result = _clear_range(access_token, spreadsheet_id, range_a1,
                          transport=transport)
    return {
        "cleared_range": result.get("clearedRange"),
        "spreadsheet_id": result.get("spreadsheetId") or spreadsheet_id,
    }


# --- sheets_add_worksheet: one new tab, sized -----------------------------------


def run_sheets_add_worksheet(action, event, *, transport=None, steps=None):
    """Add one worksheet to a spreadsheet (Sheets API ``batchUpdate``
    ``addSheet``; Zapier's Create Worksheet).

    ``title`` is the new tab's name (rendered from the event, like every
    field) and must not collide with an existing tab — Sheets rejects a
    duplicate with a 400 that surfaces as the action's error. The optional
    ``row_count``/``column_count`` size the new grid (Sheets' own defaults —
    1000 rows by 26 columns — apply when omitted); both must be positive
    whole numbers. Output: ``{sheet_id, title, row_count, column_count,
    spreadsheet_id}`` — the numeric ``sheet_id`` is what the
    ``deleteDimension`` machinery addresses, the ``title`` what the values
    actions' ranges use, so a follow-up sheets_append_row can chain on
    either.
    """
    connection = _sheets_connection(action["connection_id"])
    access_token, _info = tokens.get_access_token(connection, transport=transport)
    spreadsheet_id = _rendered_field(action, "spreadsheet_id", event, steps)
    if not spreadsheet_id:
        raise ValueError("sheets_add_worksheet requires a spreadsheet_id")
    title = _rendered_field(action, "title", event, steps)
    if not title:
        raise ValueError("sheets_add_worksheet requires a title")
    counts = {}
    for key, default in (("row_count", 1000), ("column_count", 26)):
        raw = _rendered_field(action, key, event, steps)
        if not raw:
            counts[key] = default
            continue
        try:
            value = int(raw)
        except ValueError:
            raise ValueError(
                f"sheets_add_worksheet {key} must be a whole number") from None
        if value < 1:
            raise ValueError(f"sheets_add_worksheet {key} must be at least 1")
        counts[key] = value
    properties = {
        "title": title,
        "gridProperties": {"rowCount": counts["row_count"],
                           "columnCount": counts["column_count"]},
    }
    url = (
        "https://sheets.googleapis.com/v4/spreadsheets/"
        f"{urllib.parse.quote(spreadsheet_id, safe='')}:batchUpdate"
    )
    headers = {
        "authorization": f"Bearer {access_token}",
        "content-type": "application/json",
    }
    payload = {"requests": [{"addSheet": {"properties": properties}}]}
    try:
        status, response = transport(
            "POST", url, headers=headers, body=json.dumps(payload).encode(),
            timeout=15,
        )
    except Exception as exc:
        raise RuntimeError(f"google sheets unreachable: {type(exc).__name__}")
    if status >= 300:
        raise RuntimeError(
            f"google sheets add worksheet returned HTTP {status}{_error_detail(response)}")
    try:
        result = json.loads(response.decode() or "{}")
    except (ValueError, UnicodeDecodeError):
        raise RuntimeError("google sheets returned an unreadable body") from None
    replies = result.get("replies") if isinstance(result.get("replies"), list) else []
    added = {}
    for reply in replies:
        if isinstance(reply, dict) and isinstance(reply.get("addSheet"), dict):
            added = reply["addSheet"]
            break
    added_properties = added.get("properties") if isinstance(added.get("properties"), dict) else {}
    grid = (added_properties.get("gridProperties")
            if isinstance(added_properties.get("gridProperties"), dict) else {})
    return {
        "sheet_id": added_properties.get("sheetId"),
        "title": added_properties.get("title") or title,
        "row_count": grid.get("rowCount") or counts["row_count"],
        "column_count": grid.get("columnCount") or counts["column_count"],
        "spreadsheet_id": spreadsheet_id,
    }


# --- sheets_create_column: one header cell on the header row ---------------------


def _a1_letters(index):
    """A 0-based column index as its A1 letters (0 -> "A", 26 -> "AA")."""
    letters = ""
    while True:
        letters = chr(ord("A") + index % 26) + letters
        index = index // 26 - 1
        if index < 0:
            return letters


def _header_row(access_token, spreadsheet_id, worksheet, *, transport=None):
    """The worksheet's header row, from one ``values.get`` on row 1."""
    transport = transport or base._default_transport
    range_a1 = urllib.parse.quote(f"{worksheet}!1:1", safe="")
    url = (
        "https://sheets.googleapis.com/v4/spreadsheets/"
        f"{urllib.parse.quote(spreadsheet_id, safe='')}/values/{range_a1}"
    )
    headers = {"authorization": f"Bearer {access_token}"}
    try:
        status, response = transport("GET", url, headers=headers, body=None,
                                     timeout=15)
    except Exception as exc:
        raise RuntimeError(f"google sheets unreachable: {type(exc).__name__}")
    if status >= 300:
        raise RuntimeError(
            f"google sheets read returned HTTP {status}{_error_detail(response)}")
    try:
        result = json.loads(response.decode() or "{}")
    except (ValueError, UnicodeDecodeError):
        return []
    values = result.get("values") if isinstance(result, dict) else None
    if (isinstance(values, list) and values
            and isinstance(values[0], list)):
        return values[0]
    return []


def run_sheets_create_column(action, event, *, transport=None, steps=None):
    """Append one header cell to the worksheet's header row (Zapier's Create
    Spreadsheet Column).

    Reads the header row (``values.get`` ``{worksheet}!1:1``), takes the
    first free column, and writes ``column`` there with a single-cell
    ``values.update`` (``{worksheet}!AA1``-style range — ``_a1_letters``
    spells any index, past the 26-column boundary included). A name the
    header row already has (trimmed, case-insensitively, like
    :func:`run_sheets_find_row`) is a verdict, not a duplicate: the
    existing position comes back with ``created: False`` and nothing is
    written. Output: ``{spreadsheet_id, worksheet, column, position, cell,
    created}`` — ``position`` the 1-based column number, ``cell`` the A1
    address (``AA1``).
    """
    connection = _sheets_connection(action["connection_id"])
    access_token, _info = tokens.get_access_token(connection, transport=transport)
    spreadsheet_id = _rendered_field(action, "spreadsheet_id", event, steps)
    if not spreadsheet_id:
        raise ValueError("sheets_create_column requires a spreadsheet_id")
    worksheet = str(action.get("worksheet") or "").strip() or "Sheet1"
    column = _rendered_field(action, "column", event, steps)
    if not column:
        raise ValueError("sheets_create_column requires a column")
    header = _header_row(access_token, spreadsheet_id, worksheet,
                         transport=transport)
    existing = next(
        (index for index, cell in enumerate(header)
         if str(cell).strip().lower() == column.lower()), None)
    if existing is not None:
        return {"spreadsheet_id": spreadsheet_id, "worksheet": worksheet,
                "column": str(header[existing]).strip(),
                "position": existing + 1, "cell": f"{_a1_letters(existing)}1",
                "created": False}
    free = next((index for index, cell in enumerate(header)
                 if not str(cell).strip()), len(header))
    _update_range(access_token, spreadsheet_id, f"{worksheet}!{_a1_letters(free)}1",
                  [column], "USER_ENTERED", transport=transport)
    return {"spreadsheet_id": spreadsheet_id, "worksheet": worksheet,
            "column": column, "position": free + 1,
            "cell": f"{_a1_letters(free)}1", "created": True}
