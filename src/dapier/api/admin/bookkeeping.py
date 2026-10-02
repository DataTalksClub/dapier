"""Operator console endpoints: the bookkeeping review queue.

Thin JSON wrappers over :mod:`dapier.bookkeeping_store`, shared verbatim
with the ``/api/agent/*`` routes (the CLI drives the same behavior). Reads
list/get; the writes — confirm with optional field corrections, reject —
are audited like every other settings mutation.
"""
import json

from ... import http
from ... import bookkeeping_store
from ...auth import session


def list_bookkeeping(event):
    """GET /api/admin/bookkeeping?status=&limit= — the queue plus counts."""
    query = event.get("queryStringParameters") or {}
    try:
        entries = bookkeeping_store.list_entries(
            status=query.get("status") or None, limit=query.get("limit"))
    except bookkeeping_store.BookkeepingError as exc:
        return http._json_response(400, {"error": str(exc)})
    return http._json_response(200, {
        "entries": entries,
        "counts": bookkeeping_store.counts(),
    })


def get_bookkeeping(event, entry_id):
    """GET /api/admin/bookkeeping/{id} — one entry."""
    entry = bookkeeping_store.get_entry(entry_id)
    if entry is None:
        return http._json_response(404, {"error": f"no bookkeeping entry {entry_id}"})
    return http._json_response(200, entry)


def _body(event):
    try:
        body = json.loads(event.get("body") or "{}")
    except (ValueError, TypeError):
        return None
    return body if isinstance(body, dict) else None


def confirm_bookkeeping(event, entry_id, operator):
    """POST /api/admin/bookkeeping/{id}/confirm — confirm, with optional
    reviewer corrections ({"edits": {field: value}})."""
    body = _body(event) or {}
    try:
        entry = bookkeeping_store.confirm_entry(
            entry_id, edits=body.get("edits") or {}, confirmed_by=operator or "")
    except bookkeeping_store.BookkeepingError as exc:
        message = str(exc)
        status = 404 if message.startswith("no bookkeeping entry") else 400
        return http._json_response(status, {"error": message})
    session._audit_event("bookkeeping", "entry.confirm", operator or "unknown",
                         outcome="ok")
    return http._json_response(200, entry)


def reject_bookkeeping(event, entry_id, operator):
    """POST /api/admin/bookkeeping/{id}/reject — reject, with an optional
    note. The invoice can be re-staged by forwarding it again."""
    body = _body(event) or {}
    try:
        entry = bookkeeping_store.reject_entry(
            entry_id, rejected_by=operator or "", note=body.get("note"))
    except bookkeeping_store.BookkeepingError as exc:
        message = str(exc)
        status = 404 if message.startswith("no bookkeeping entry") else 400
        return http._json_response(status, {"error": message})
    session._audit_event("bookkeeping", "entry.reject", operator or "unknown",
                         outcome="ok")
    return http._json_response(200, entry)
