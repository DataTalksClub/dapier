"""csv_parse / csv_format actions: CSV text in and out of the chain.

``csv_parse`` reads exactly one source — inline ``content`` or a staged
``source_s3`` ``{bucket, key}`` object (the same dialect s3_upload and
slack_upload_file source files with) — and emits ``{headers, rows, count}``.
Rows are dicts keyed by the header row when ``header_row`` is on (the
default), plain lists otherwise, and values stay strings exactly as the
file wrote them. ``csv_format`` is the mirror: a template-rendered ``rows``
list of dicts (or of lists) plus an optional explicit ``headers`` order
(default: the first row's keys, insertion order) serializes back to CSV
text, so a chain can hand a provider's JSON to anything that eats CSV.
"""
import csv
import io
import json

from . import base
from .templating import render


def _delimiter(action, event, steps):
    raw = render(str(action.get("delimiter") or ""), event, steps).strip()
    return raw or ","


def _header_row(action, event, steps, default=True):
    value = action.get("header_row")
    if value is None or (isinstance(value, str) and not value.strip()):
        return default
    raw = render(str(value), event, steps).strip()
    if not raw:
        return default
    return raw.lower() in ("true", "1", "yes", "on")


def _csv_text(action, event, steps):
    """The CSV text from exactly one source: inline content or a staged
    ``source_s3`` object. ``source_s3`` bucket/key take templates, so an
    earlier step's staged file (s3_read_object, say) feeds the parse."""
    content = action.get("content")
    source = action.get("source_s3") if isinstance(action.get("source_s3"), dict) else {}
    staged = {
        "bucket": render(str(source.get("bucket") or ""), event, steps).strip(),
        "key": render(str(source.get("key") or ""), event, steps).strip(),
    }
    if content and staged["bucket"]:
        raise ValueError("csv_parse takes either content or source_s3, not both")
    if content:
        return render(str(content), event, steps)
    if staged["bucket"] and staged["key"]:
        # utf-8-sig swallows the BOM spreadsheet exports prepend.
        return base._s3_body(staged).decode("utf-8-sig", errors="replace")
    raise ValueError("csv_parse needs content or source_s3 with bucket and key")


def run_csv_parse(action, event, workflow_id=None, *, steps=None):
    text = _csv_text(action, event, steps)
    delimiter = _delimiter(action, event, steps)
    header_row = _header_row(action, event, steps)
    rows = [row for row in csv.reader(io.StringIO(text), delimiter=delimiter) if row]
    headers = [cell.strip() for cell in rows[0]] if header_row and rows else []
    body = rows[1:] if headers else rows
    if headers:
        body = [
            {header: (row[index] if index < len(row) else "")
             for index, header in enumerate(headers)}
            for row in body
        ]
    return {"headers": headers, "rows": body, "count": len(body)}


def _format_rows(action, event, steps):
    """The ``rows`` field as a list of dicts or lists, every leaf string
    template-rendered (the sheets actions' parsing idiom)."""
    raw = action.get("rows")
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError:
            raise ValueError("rows must be a JSON array of objects or arrays") from None
    if not isinstance(raw, list):
        raise ValueError("rows must be a JSON array of objects or arrays")

    def rendered(value):
        if isinstance(value, str):
            return render(value, event, steps)
        if isinstance(value, dict):
            return {key: rendered(item) for key, item in value.items()}
        if isinstance(value, list):
            return [rendered(item) for item in value]
        return value

    rows = rendered(raw)
    if any(not isinstance(row, (dict, list)) for row in rows):
        raise ValueError("rows must be objects (keyed by header) or arrays")
    return rows


def run_csv_format(action, event, workflow_id=None, *, steps=None):
    rows = _format_rows(action, event, steps)
    headers = action.get("headers")
    if isinstance(headers, str):
        try:
            headers = json.loads(headers)
        except ValueError:
            headers = [cell.strip() for cell in headers.split(",") if cell.strip()]
    if headers is None:
        first = next((row for row in rows if isinstance(row, dict)), None)
        headers = list(first) if first else []
    if not isinstance(headers, list):
        raise ValueError("headers must be a JSON array of column names")
    buffer = io.StringIO()
    writer = csv.writer(buffer, delimiter=_delimiter(action, event, steps))
    if headers:
        writer.writerow(headers)
    for row in rows:
        if isinstance(row, dict):
            writer.writerow([row.get(header, "") for header in headers])
        else:
            writer.writerow(row)
    return {"csv": buffer.getvalue(), "count": len(rows)}
