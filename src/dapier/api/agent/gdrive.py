"""Operator Google Drive proxy: the `dapier gdrive` CLI's routes over the
ops layer. Same gating/audit/account rules as the Gmail routes: every
operation names its account (query on GET, body on writes), resolved by
``google_accounts`` before the provider sees the call.
"""

import json
import re

from . import gdrive_ops
from .common import _LateBinding, _json_response
from .google_accounts import GoogleAccountError

require_operator = _LateBinding("require_operator")
audit = _LateBinding("audit")


def _error(exc):
    status = getattr(exc, "status", 400) or 400
    message = str(exc) or "Drive request failed"
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
    subject, error = require_operator(event, "gdrive.accounts")
    if error:
        return error
    return _json_response(200, gdrive_ops.accounts())


def list_files_api(event):
    subject, error = require_operator(event, "gdrive.list_files")
    if error:
        return error
    query = event.get("queryStringParameters") or {}
    try:
        payload = gdrive_ops.list_files(
            _account(event),
            query=query.get("q") or query.get("query"),
            folder_id=query.get("folder"),
            max_results=int(query.get("max") or query.get("limit") or 0) or gdrive_ops.DEFAULT_PAGE_SIZE,
            page_token=query.get("page_token"))
    except (gdrive_ops.GDriveOpsError, GoogleAccountError, ValueError) as exc:
        return _error(exc)
    _audit(payload.get("account") or "gdrive", "gdrive.list_files", subject)
    return _json_response(200, payload)


def get_file_api(event, file_id):
    subject, error = require_operator(event, "gdrive.get_file")
    if error:
        return error
    try:
        payload = gdrive_ops.get_file(_account(event), file_id)
    except (gdrive_ops.GDriveOpsError, GoogleAccountError) as exc:
        return _error(exc)
    _audit(payload.get("id") or "gdrive", "gdrive.get_file", subject)
    return _json_response(200, payload)


def download_api(event, file_id):
    subject, error = require_operator(event, "gdrive.download")
    if error:
        return error
    query = event.get("queryStringParameters") or {}
    try:
        payload = gdrive_ops.download(
            _account(event), file_id, export_mime=query.get("export_mime"))
    except (gdrive_ops.GDriveOpsError, GoogleAccountError) as exc:
        return _error(exc)
    _audit(payload.get("file_id") or "gdrive", "gdrive.download", subject)
    return _json_response(200, payload)


def upload_api(event):
    subject, error = require_operator(event, "gdrive.upload")
    if error:
        return error
    body = _json_body(event)
    if body is None:
        return _json_response(400, {"error": "Invalid JSON body"})
    try:
        payload = gdrive_ops.upload(
            _account(event, body), body.get("name"), body.get("content_b64"),
            mime_type=body.get("mime_type"), folder_id=body.get("folder_id"))
    except (gdrive_ops.GDriveOpsError, GoogleAccountError) as exc:
        return _error(exc)
    _audit(payload.get("id") or "gdrive", "gdrive.upload", subject)
    return _json_response(200, payload)


def update_file_api(event, file_id):
    subject, error = require_operator(event, "gdrive.update")
    if error:
        return error
    body = _json_body(event)
    if body is None:
        return _json_response(400, {"error": "Invalid JSON body"})
    try:
        payload = gdrive_ops.update(
            _account(event, body), file_id, name=body.get("name"),
            content_b64=body.get("content_b64"), mime_type=body.get("mime_type"))
    except (gdrive_ops.GDriveOpsError, GoogleAccountError) as exc:
        return _error(exc)
    _audit(payload.get("id") or "gdrive", "gdrive.update", subject)
    return _json_response(200, payload)


def copy_file_api(event, file_id):
    subject, error = require_operator(event, "gdrive.copy")
    if error:
        return error
    body = _json_body(event) or {}
    try:
        payload = gdrive_ops.copy(
            _account(event, body), file_id, name=body.get("name"))
    except (gdrive_ops.GDriveOpsError, GoogleAccountError) as exc:
        return _error(exc)
    _audit(payload.get("id") or "gdrive", "gdrive.copy", subject)
    return _json_response(200, payload)


def move_file_api(event, file_id):
    subject, error = require_operator(event, "gdrive.move")
    if error:
        return error
    body = _json_body(event) or {}
    try:
        payload = gdrive_ops.move(
            _account(event, body), file_id, add_parent=body.get("add_parent"),
            remove_parent=body.get("remove_parent"))
    except (gdrive_ops.GDriveOpsError, GoogleAccountError) as exc:
        return _error(exc)
    _audit(payload.get("id") or "gdrive", "gdrive.move", subject)
    return _json_response(200, payload)


def share_file_api(event, file_id):
    subject, error = require_operator(event, "gdrive.share")
    if error:
        return error
    body = _json_body(event) or {}
    try:
        payload = gdrive_ops.share(
            _account(event, body), file_id, role=body.get("role") or "reader",
            share_type=body.get("share_type") or "user",
            email_address=body.get("email_address"))
    except (gdrive_ops.GDriveOpsError, GoogleAccountError) as exc:
        return _error(exc)
    _audit(payload.get("file_id") or "gdrive", "gdrive.share", subject)
    return _json_response(200, payload)


def trash_file_api(event, file_id):
    subject, error = require_operator(event, "gdrive.trash")
    if error:
        return error
    body = _json_body(event) or {}
    try:
        payload = gdrive_ops.trash(
            _account(event, body), file_id, permanent=bool(body.get("permanent")))
    except (gdrive_ops.GDriveOpsError, GoogleAccountError) as exc:
        return _error(exc)
    _audit(payload.get("file_id") or "gdrive", "gdrive.trash", subject)
    return _json_response(200, payload)


def restore_file_api(event, file_id):
    subject, error = require_operator(event, "gdrive.restore")
    if error:
        return error
    body = _json_body(event) or {}
    try:
        payload = gdrive_ops.restore(_account(event, body), file_id)
    except (gdrive_ops.GDriveOpsError, GoogleAccountError) as exc:
        return _error(exc)
    _audit(payload.get("file_id") or "gdrive", "gdrive.restore", subject)
    return _json_response(200, payload)


def create_folder_api(event):
    subject, error = require_operator(event, "gdrive.create_folder")
    if error:
        return error
    body = _json_body(event)
    if body is None:
        return _json_response(400, {"error": "Invalid JSON body"})
    try:
        payload = gdrive_ops.create_folder(
            _account(event, body), body.get("name"), parent_id=body.get("parent_id"))
    except (gdrive_ops.GDriveOpsError, GoogleAccountError) as exc:
        return _error(exc)
    _audit(payload.get("id") or "gdrive", "gdrive.create_folder", subject)
    return _json_response(200, payload)


_ID = r"([A-Za-z0-9_.=-]+)"


def route_gdrive(event, method, path):
    """Return a response for Drive agent routes, or None if unmatched."""
    if path == "/api/agent/gdrive/accounts" and method == "GET":
        return accounts_api(event)
    if path == "/api/agent/gdrive/files" and method == "GET":
        return list_files_api(event)
    if path == "/api/agent/gdrive/files" and method == "POST":
        return upload_api(event)
    if path == "/api/agent/gdrive/folders" and method == "POST":
        return create_folder_api(event)
    match = re.fullmatch(r"/api/agent/gdrive/files/" + _ID, path)
    if match and method == "GET":
        return get_file_api(event, match.group(1))
    if match and method == "PATCH":
        return update_file_api(event, match.group(1))
    content_match = re.fullmatch(r"/api/agent/gdrive/files/" + _ID + r"/content", path)
    if content_match and method == "GET":
        return download_api(event, content_match.group(1))
    copy_match = re.fullmatch(r"/api/agent/gdrive/files/" + _ID + r"/copy", path)
    if copy_match and method == "POST":
        return copy_file_api(event, copy_match.group(1))
    move_match = re.fullmatch(r"/api/agent/gdrive/files/" + _ID + r"/move", path)
    if move_match and method == "POST":
        return move_file_api(event, move_match.group(1))
    share_match = re.fullmatch(r"/api/agent/gdrive/files/" + _ID + r"/share", path)
    if share_match and method == "POST":
        return share_file_api(event, share_match.group(1))
    trash_match = re.fullmatch(r"/api/agent/gdrive/files/" + _ID + r"/trash", path)
    if trash_match and method == "POST":
        return trash_file_api(event, trash_match.group(1))
    restore_match = re.fullmatch(r"/api/agent/gdrive/files/" + _ID + r"/restore", path)
    if restore_match and method == "POST":
        return restore_file_api(event, restore_match.group(1))
    return None
