"""Operator Gmail proxy: the `dapier gmail` CLI's routes over the ops layer.

Every route is operator-gated and audited like the Slack ones; the
``account`` selector (the CLI's ``--account``) rides the query string on
GETs and the JSON body on writes, and is resolved by ``google_accounts``
before any provider call — never silently defaulted when several Google
accounts serve Gmail.
"""

import json

from . import gmail_ops
from .common import _LateBinding, _json_response
from .google_accounts import GoogleAccountError

require_operator = _LateBinding("require_operator")
audit = _LateBinding("audit")


def _error(exc):
    status = getattr(exc, "status", 400) or 400
    message = str(exc) or "Gmail request failed"
    return _json_response(status, {"error": message})


def _account(event, body=None):
    query = event.get("queryStringParameters") or {}
    raw = query.get("account")
    if isinstance(body, dict):
        raw = raw or body.get("account")
    return str(raw or "").strip() or None


def _json_body(event):
    try:
        body = json.loads(event.get("body") or "{}")
    except (ValueError, TypeError):
        return None
    return body if isinstance(body, dict) else None


def _audit(connection_id, action, subject):
    audit.emit(connection_id, action, subject, outcome="ok")


def accounts_api(event):
    subject, error = require_operator(event, "gmail.accounts")
    if error:
        return error
    return _json_response(200, gmail_ops.accounts())


def list_messages_api(event):
    subject, error = require_operator(event, "gmail.list_messages")
    if error:
        return error
    query = event.get("queryStringParameters") or {}
    try:
        payload = gmail_ops.list_messages(
            _account(event),
            query=query.get("q") or query.get("query"),
            label_ids=[label for label in (query.get("label") or "").split(",") if label],
            max_results=int(query.get("max") or query.get("limit") or 0) or gmail_ops.DEFAULT_PAGE_SIZE,
            page_token=query.get("page_token"),
        )
    except (gmail_ops.GmailOpsError, GoogleAccountError, ValueError) as exc:
        return _error(exc)
    _audit(payload.get("account") or "gmail", "gmail.list_messages", subject)
    return _json_response(200, payload)


def get_message_api(event, message_id):
    subject, error = require_operator(event, "gmail.get_message")
    if error:
        return error
    query = event.get("queryStringParameters") or {}
    try:
        payload = gmail_ops.get_message(
            _account(event), message_id,
            include_html=str(query.get("html") or "").lower() in ("1", "true", "yes"))
    except (gmail_ops.GmailOpsError, GoogleAccountError) as exc:
        return _error(exc)
    _audit(payload.get("account") or "gmail", "gmail.get_message", subject)
    return _json_response(200, payload)


def get_attachment_api(event, message_id, attachment_id):
    subject, error = require_operator(event, "gmail.get_attachment")
    if error:
        return error
    try:
        payload = gmail_ops.get_attachment(_account(event), message_id, attachment_id)
    except (gmail_ops.GmailOpsError, GoogleAccountError) as exc:
        return _error(exc)
    _audit(_account(event) or "gmail", "gmail.get_attachment", subject)
    return _json_response(200, payload)


def list_threads_api(event):
    subject, error = require_operator(event, "gmail.list_threads")
    if error:
        return error
    query = event.get("queryStringParameters") or {}
    try:
        payload = gmail_ops.list_threads(
            _account(event),
            query=query.get("q") or query.get("query"),
            max_results=int(query.get("max") or query.get("limit") or 0) or gmail_ops.DEFAULT_PAGE_SIZE,
            page_token=query.get("page_token"),
        )
    except (gmail_ops.GmailOpsError, GoogleAccountError, ValueError) as exc:
        return _error(exc)
    _audit(payload.get("account") or "gmail", "gmail.list_threads", subject)
    return _json_response(200, payload)


def get_thread_api(event, thread_id):
    subject, error = require_operator(event, "gmail.get_thread")
    if error:
        return error
    try:
        payload = gmail_ops.get_thread(_account(event), thread_id)
    except (gmail_ops.GmailOpsError, GoogleAccountError) as exc:
        return _error(exc)
    _audit(payload.get("account") or "gmail", "gmail.get_thread", subject)
    return _json_response(200, payload)


def list_labels_api(event):
    subject, error = require_operator(event, "gmail.list_labels")
    if error:
        return error
    try:
        payload = gmail_ops.list_labels(_account(event))
    except (gmail_ops.GmailOpsError, GoogleAccountError) as exc:
        return _error(exc)
    _audit(payload.get("account") or "gmail", "gmail.list_labels", subject)
    return _json_response(200, payload)


def modify_api(event, message_id):
    subject, error = require_operator(event, "gmail.modify")
    if error:
        return error
    body = _json_body(event)
    if body is None:
        return _json_response(400, {"error": "Invalid JSON body"})
    try:
        payload = gmail_ops.modify(
            _account(event, body), message_id,
            add_labels=body.get("add_labels") or [],
            remove_labels=body.get("remove_labels") or [],
            mark_read=bool(body.get("mark_read")),
            mark_unread=bool(body.get("mark_unread")),
            archive=bool(body.get("archive")),
            trash=bool(body.get("trash")),
            untrash=bool(body.get("untrash")))
    except (gmail_ops.GmailOpsError, GoogleAccountError) as exc:
        return _error(exc)
    _audit(payload.get("account") or "gmail", "gmail.modify", subject)
    return _json_response(200, payload)


def send_api(event):
    subject, error = require_operator(event, "gmail.send")
    if error:
        return error
    body = _json_body(event)
    if body is None:
        return _json_response(400, {"error": "Invalid JSON body"})
    try:
        payload = gmail_ops.send(
            _account(event, body), to=body.get("to"), cc=body.get("cc"),
            bcc=body.get("bcc"), subject=body.get("subject"),
            text=body.get("text"), html=body.get("html"),
            attachments=body.get("attachments"))
    except (gmail_ops.GmailOpsError, GoogleAccountError) as exc:
        return _error(exc)
    _audit(payload.get("account") or "gmail", "gmail.send", subject)
    return _json_response(200, payload)


def reply_api(event):
    subject, error = require_operator(event, "gmail.reply")
    if error:
        return error
    body = _json_body(event)
    if body is None:
        return _json_response(400, {"error": "Invalid JSON body"})
    message_id = str(body.get("message_id") or body.get("message") or "").strip()
    if not message_id:
        return _json_response(400, {"error": "message_id of the message to reply to is required"})
    try:
        payload = gmail_ops.reply(
            _account(event, body), message_id,
            reply_all=bool(body.get("reply_all")),
            text=body.get("text"), html=body.get("html"),
            attachments=body.get("attachments"),
            draft=bool(body.get("draft")))
    except (gmail_ops.GmailOpsError, GoogleAccountError) as exc:
        return _error(exc)
    _audit(payload.get("account") or "gmail",
           "gmail.reply_draft" if payload.get("draft") else "gmail.reply", subject)
    return _json_response(200, payload)


def list_drafts_api(event):
    subject, error = require_operator(event, "gmail.list_drafts")
    if error:
        return error
    query = event.get("queryStringParameters") or {}
    try:
        payload = gmail_ops.list_drafts(
            _account(event),
            max_results=int(query.get("max") or query.get("limit") or 0) or gmail_ops.DEFAULT_PAGE_SIZE,
            page_token=query.get("page_token"))
    except (gmail_ops.GmailOpsError, GoogleAccountError, ValueError) as exc:
        return _error(exc)
    _audit(payload.get("account") or "gmail", "gmail.list_drafts", subject)
    return _json_response(200, payload)


def create_draft_api(event):
    subject, error = require_operator(event, "gmail.create_draft")
    if error:
        return error
    body = _json_body(event)
    if body is None:
        return _json_response(400, {"error": "Invalid JSON body"})
    try:
        payload = gmail_ops.create_draft(
            _account(event, body), to=body.get("to"), cc=body.get("cc"),
            bcc=body.get("bcc"), subject=body.get("subject"),
            text=body.get("text"), html=body.get("html"),
            attachments=body.get("attachments"))
    except (gmail_ops.GmailOpsError, GoogleAccountError) as exc:
        return _error(exc)
    _audit(payload.get("account") or "gmail", "gmail.create_draft", subject)
    return _json_response(200, payload)


def get_draft_api(event, draft_id):
    subject, error = require_operator(event, "gmail.get_draft")
    if error:
        return error
    try:
        payload = gmail_ops.get_draft(_account(event), draft_id)
    except (gmail_ops.GmailOpsError, GoogleAccountError) as exc:
        return _error(exc)
    _audit(payload.get("account") or "gmail", "gmail.get_draft", subject)
    return _json_response(200, payload)


def update_draft_api(event, draft_id):
    subject, error = require_operator(event, "gmail.update_draft")
    if error:
        return error
    body = _json_body(event)
    if body is None:
        return _json_response(400, {"error": "Invalid JSON body"})
    try:
        payload = gmail_ops.update_draft(
            _account(event, body), draft_id, to=body.get("to"), cc=body.get("cc"),
            bcc=body.get("bcc"), subject=body.get("subject"),
            text=body.get("text"), html=body.get("html"),
            attachments=body.get("attachments"))
    except (gmail_ops.GmailOpsError, GoogleAccountError) as exc:
        return _error(exc)
    _audit(payload.get("account") or "gmail", "gmail.update_draft", subject)
    return _json_response(200, payload)


def delete_draft_api(event, draft_id):
    subject, error = require_operator(event, "gmail.delete_draft")
    if error:
        return error
    try:
        payload = gmail_ops.delete_draft(_account(event), draft_id)
    except (gmail_ops.GmailOpsError, GoogleAccountError) as exc:
        return _error(exc)
    _audit(payload.get("account") or "gmail", "gmail.delete_draft", subject)
    return _json_response(200, payload)


def send_draft_api(event, draft_id):
    subject, error = require_operator(event, "gmail.send_draft")
    if error:
        return error
    body = _json_body(event) or {}
    try:
        payload = gmail_ops.send_draft(_account(event, body), draft_id)
    except (gmail_ops.GmailOpsError, GoogleAccountError) as exc:
        return _error(exc)
    _audit(payload.get("account") or "gmail", "gmail.send_draft", subject)
    return _json_response(200, payload)


import re  # noqa: E402

_ID = r"([A-Za-z0-9_-]+)"


def route_gmail(event, method, path):
    """Return a response for Gmail agent routes, or None if unmatched."""
    if path == "/api/agent/gmail/accounts" and method == "GET":
        return accounts_api(event)
    if path == "/api/agent/gmail/messages" and method == "GET":
        return list_messages_api(event)
    if path == "/api/agent/gmail/threads" and method == "GET":
        return list_threads_api(event)
    if path == "/api/agent/gmail/labels" and method == "GET":
        return list_labels_api(event)
    if path == "/api/agent/gmail/drafts" and method == "GET":
        return list_drafts_api(event)
    if path == "/api/agent/gmail/drafts" and method == "POST":
        return create_draft_api(event)
    if path == "/api/agent/gmail/send" and method == "POST":
        return send_api(event)
    if path == "/api/agent/gmail/reply" and method == "POST":
        return reply_api(event)
    match = re.fullmatch(r"/api/agent/gmail/messages/" + _ID, path)
    if match and method == "GET":
        return get_message_api(event, match.group(1))
    attach_match = re.fullmatch(r"/api/agent/gmail/messages/" + _ID + r"/attachments/" + _ID, path)
    if attach_match and method == "GET":
        return get_attachment_api(event, attach_match.group(1), attach_match.group(2))
    modify_match = re.fullmatch(r"/api/agent/gmail/messages/" + _ID + r"/modify", path)
    if modify_match and method == "POST":
        return modify_api(event, modify_match.group(1))
    thread_match = re.fullmatch(r"/api/agent/gmail/threads/" + _ID, path)
    if thread_match and method == "GET":
        return get_thread_api(event, thread_match.group(1))
    draft_match = re.fullmatch(r"/api/agent/gmail/drafts/" + _ID, path)
    if draft_match and method == "GET":
        return get_draft_api(event, draft_match.group(1))
    if draft_match and method == "PUT":
        return update_draft_api(event, draft_match.group(1))
    if draft_match and method == "DELETE":
        return delete_draft_api(event, draft_match.group(1))
    draft_send_match = re.fullmatch(r"/api/agent/gmail/drafts/" + _ID + r"/send", path)
    if draft_send_match and method == "POST":
        return send_draft_api(event, draft_send_match.group(1))
    return None
