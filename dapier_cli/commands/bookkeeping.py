"""Implementations of the `dapier bookkeeping` commands."""

import json
from urllib.parse import quote, urlencode

from .. import api

__all__ = ["bookkeeping_confirm", "bookkeeping_get", "bookkeeping_list",
           "bookkeeping_reject"]

_SUMMARY_COLUMNS = ("created_at", "status", "source", "provider",
                    "invoice_number", "what", "amount", "currency")


def _entry_field(entry, field):
    """Summary fields read from the entry payload or the item itself."""
    nested = (entry.get("entry") or {}).get(field)
    if nested is not None:
        return nested
    return entry.get(field, "")


def _edit_value(raw):
    """`KEY=VALUE` right-hand side: JSON when it parses, else the string."""
    try:
        return json.loads(raw)
    except ValueError:
        return raw


def bookkeeping_list(api_url, status=None, limit=None, debug=False):
    query = {}
    if status:
        query["status"] = status
    if limit:
        query["limit"] = int(limit)
    suffix = f"?{urlencode(query)}" if query else ""
    data = api.call(api_url, "GET", f"/api/agent/bookkeeping{suffix}", debug=debug)
    entries = data.get("entries", [])
    counts = data.get("counts") or {}
    if counts:
        print("queue: " + ", ".join(f"{key} {counts[key]}"
                                    for key in ("pending", "confirmed", "rejected")
                                    if key in counts))
    if not entries:
        print("No bookkeeping entries." + (" Wait for the invoice workflow to stage one."
                                           if not status else ""))
        return 0
    print(f"{'ENTRY':34} {'STATUS':10} {'SOURCE':14} {'PROVIDER':16} "
          f"{'INVOICE':24} AMOUNT")
    for entry in entries:
        print(f"{entry.get('entry_id', ''):34} "
              f"{entry.get('status', ''):10} "
              f"{entry.get('source', '') or '-':14} "
              f"{str(_entry_field(entry, 'provider'))[:16]:16} "
              f"{str(_entry_field(entry, 'invoice_number'))[:24]:24} "
              f"{_entry_field(entry, 'currency') or ''} "
              f"{_entry_field(entry, 'amount') or '?'}")
    return 0


def bookkeeping_get(api_url, entry_id, debug=False):
    data = api.call(api_url, "GET",
                    f"/api/agent/bookkeeping/{quote(entry_id, safe='')}", debug=debug)
    for key in ("entry_id", "status", "source", "workflow_id", "created_at"):
        if data.get(key):
            print(f"{key}: {data[key]}")
    print("entry:")
    for key, value in sorted((data.get("entry") or {}).items()):
        print(f"  {key}: {value}")
    message = data.get("message") or {}
    if message:
        print("message: " + " | ".join(
            f"{key}: {value}" for key, value in sorted(message.items()) if value))
    pdf = data.get("pdf") or {}
    if pdf.get("s3"):
        print(f"pdf: {pdf.get('filename')} (s3://{pdf['s3'].get('bucket')}/"
              f"{pdf['s3'].get('key')})")
    return 0


def bookkeeping_confirm(api_url, entry_id, edits=None, debug=False):
    body = {"edits": {}}
    for spec in edits or []:
        key, _, value = spec.partition("=")
        if not key or not _:
            raise SystemExit(f"--edit expects KEY=VALUE, got {spec!r}")
        body["edits"][key] = _edit_value(value)
    data = api.call(api_url, "POST",
                    f"/api/agent/bookkeeping/{quote(entry_id, safe='')}/confirm",
                    body=body, debug=debug)
    provider = (data.get("entry") or {}).get("provider") or "?"
    number = (data.get("entry") or {}).get("invoice_number") or "?"
    print(f"Confirmed {provider} {number} ({data.get('entry_id')}) — "
          "it can reach the ledger now.")
    return 0


def bookkeeping_reject(api_url, entry_id, note=None, debug=False):
    body = {"note": note} if note else {}
    data = api.call(api_url, "POST",
                    f"/api/agent/bookkeeping/{quote(entry_id, safe='')}/reject",
                    body=body, debug=debug)
    print(f"Rejected {data.get('entry_id')}"
          + (" — forwarding the invoice again stages a fresh entry." if not note
             else f" ({note})"))
    return 0
